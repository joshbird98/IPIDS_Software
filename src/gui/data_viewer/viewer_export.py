"""Exporting data and publication figures for a time range."""
import os
import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import QFileDialog
from PyQt6.QtCore import QTimer
from src.core import export_data as export_data
from src.gui.data_viewer.export_dialog import ExportDialog, ExportWorker


class ExportMixin:
    """Exporting data and publication figures for a time range. Mixed into DataViewerApp (viewer_main.py)."""

    def _browse_export_path(self):
        current = self.edit_export_path.text()
        full_path, _ = QFileDialog.getSaveFileName(self, "Select Export Base Name", current)
        if full_path:
            self.edit_export_path.setText(full_path)

    def _export_channels(self):
        """Selected channels in lane order, with registry unit/description."""
        sel = [t for t in self.plot_config if self.plot_config[t].get('selected') and t in self.cache.tags]
        sel.sort(key=lambda t: (self.plot_config[t].get('lane', 'Lane 1'), list(self.plot_config).index(t)))
        chans = []
        for t in sel:
            cfg = self.plot_config[t]
            reg = getattr(self, 'system_registry', {}).get(t, {})
            chans.append({"tag": t, "label": str(cfg.get('label', t)), "unit": reg.get("unit", ""),
                          "lane": cfg.get('lane', 'Lane 1'), "scale": cfg.get('scale', 'linear'),
                          "multiplier": float(cfg.get('multiplier', 1.0) or 1.0),
                          "description": reg.get("description", "")})
        return export_data._unique_names(chans)

    def _displayed_frame(self, t0, t1, tags):
        """What is drawn on screen in [t0, t1] as one table: historical rows up to
        where live coverage starts, then live rows on a merged time base (union of
        the channels' sample times, each held at its latest sample)."""
        import polars as pl
        live = getattr(self, 'current_live', None) or {}
        live_start = self.cache.live.oldest_time() if live else None
        x_parts, y_parts = [], {t: [] for t in tags}
        hist_x = getattr(self, 'historical_x', np.array([]))
        if getattr(self, 'is_historical_mode', False) and len(hist_x) > 0:
            hx = hist_x + self.t0
            h_end = t1 if live_start is None else min(t1, live_start)
            m = (hx >= t0) & (hx < h_end)
            if m.any():
                x_parts.append(hx[m])
                for t in tags:
                    y = self.historical_y.get(t)
                    y_parts[t].append(y[m] if y is not None and len(y) == len(hx) else np.full(int(m.sum()), np.nan))
        live_x = [x[(x >= t0) & (x <= t1)] for t, (x, _) in live.items() if t in y_parts]
        live_x = [x for x in live_x if len(x)]
        if live_x:
            base = np.unique(np.concatenate(live_x))
            x_parts.append(base)
            for t in tags:
                if t in live:
                    x, y = live[t]
                    k = np.searchsorted(x, base, side='right') - 1
                    y_parts[t].append(np.where(k >= 0, y[np.clip(k, 0, len(y) - 1)], np.nan))
                else:
                    y_parts[t].append(np.full(len(base), np.nan))
        if not x_parts:
            return pl.DataFrame({"timestamp": np.array([], dtype=np.float64), **{t: np.array([]) for t in tags}})
        return pl.DataFrame({"timestamp": np.concatenate(x_parts), **{t: np.concatenate(v) for t, v in y_parts.items()}})

    def _render_screen_svg(self):
        """Vector snapshot of the viewer exactly as shown (must run on the GUI thread)."""
        from PyQt6 import QtSvg
        from PyQt6.QtCore import QBuffer, QIODevice, QRectF
        prev_gl = pg.getConfigOption('useOpenGL')
        pg.setConfigOptions(useOpenGL=False)
        try:
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            export_rect = self.layout_widget.ci.sceneBoundingRect()
            generator = QtSvg.QSvgGenerator()
            generator.setOutputDevice(buffer)
            generator.setResolution(96)
            generator.setSize(export_rect.size().toSize())
            generator.setViewBox(export_rect)
            painter = pg.QtGui.QPainter(generator)
            painter.fillRect(export_rect, self.layout_widget.backgroundBrush())
            self.layout_widget.scene().render(painter, QRectF(0, 0, export_rect.width(), export_rect.height()),
                                              export_rect)
            painter.end()
            data = bytes(buffer.data())
            buffer.close()
            return data
        finally:
            # restore (the old code forced useOpenGL=True for every view created afterwards)
            pg.setConfigOptions(useOpenGL=prev_gl)

    def _prepare_export(self, options=None):
        """Export button: choose range / resolution / outputs, then export in the background.
        `options` (dict as returned by ExportDialog.options) skips the dialog."""
        if not isinstance(options, dict):
            options = None   # e.g. a stray `checked` bool from a signal
        if not self.cache.tags or not self.lanes:
            return
        channels = self._export_channels()
        if not channels:
            return
        if getattr(self, 'export_worker', None) is not None and self.export_worker.isRunning():
            return
        v0, v1 = list(self.lanes.values())[0].viewRange()[0]
        t0, t1 = v0 + self.t0, v1 + self.t0
        full_path = self.edit_export_path.text()
        if options is None:
            disp_rows = sum(len(x) for x, _ in (getattr(self, 'current_live', None) or {}).values()) \
                        + len(getattr(self, 'historical_x', []))
            dlg = ExportDialog(self, t0, t1, len(channels), export_data.logger_tick_seconds(self.PROJECT_ROOT),
                               disp_rows, os.path.dirname(full_path), os.path.basename(full_path))
            if not dlg.exec():
                return
            options = dlg.options()
            self.edit_export_path.setText(os.path.join(options["dest"], options["name"]))

        markers = []
        if options.get("markers", True):
            md = self._load_markers(options["t0"], options["t1"])
            markers = sorted(({"timestamp": float(k), "type": v.get("type", ""), "text": v.get("text", ""),
                               "color": v.get("color", "")} for k, v in md.items()), key=lambda m: m["timestamp"])
        displayed = self._displayed_frame(options["t0"], options["t1"], [c["tag"] for c in channels]) \
            if options["mode"] == "displayed" else None
        svg = self._render_screen_svg() if options.get("screen_svg") else None

        self.btn_export.setEnabled(False)
        self.btn_export.setText("⏳ Exporting...")
        self.export_worker = ExportWorker(self.cache.engine, self.cache.live, self.PROJECT_ROOT, options,
                                          channels, markers, displayed, svg)
        self.export_worker.progress.connect(lambda m: self.btn_export.setText(f"⏳ {m}"))
        self.export_worker.done.connect(self._on_export_finished)
        self.export_worker.failed.connect(self._on_export_error)
        self.export_worker.start()

    def _on_export_finished(self, folder):
        print(f"[Export] Complete - {folder}")
        self.last_export_folder = folder
        self.btn_export.setEnabled(True)
        self.btn_export.setText("✅ Exported")
        QTimer.singleShot(3000, lambda: self.btn_export.setText("📸 Export Snapshot"))
        if getattr(self, '_export_quiet', False):
            return
        from PyQt6.QtWidgets import QMessageBox
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        box = QMessageBox(QMessageBox.Icon.Information, "Export complete", f"Saved to:\n{folder}",
                          parent=self)
        open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.setModal(False)
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
        box.show()

    def _on_export_error(self, err):
        print(f"[Export] Error: {err}")
        self.btn_export.setEnabled(True)
        self.btn_export.setText("❌ Export failed")
        QTimer.singleShot(3000, lambda: self.btn_export.setText("📸 Export Snapshot"))
        if not getattr(self, '_export_quiet', False):
            from PyQt6.QtWidgets import QMessageBox
            box = QMessageBox(QMessageBox.Icon.Warning, "Export failed", str(err), parent=self)
            box.setModal(False)
            box.show()
