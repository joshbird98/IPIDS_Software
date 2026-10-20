"""
Export dialog + background worker for the data viewer.

Every export goes to its own folder:
    <destination>/<name>_<YYYYmmdd_HHMMSS>/
        data.csv, data.parquet       the table (see src/core/export_data.py)
        metadata.json                range, mode, channels, units, sources, markers
        summary.csv                  per-channel N / mean / std / min / max / first / last
        caption.txt                  draft figure caption
        markers.csv                  event markers in the range
        figure.pdf / .svg / .png     publication figure (light theme, vector)
        make_figure.py               standalone script that regenerates the figure
        screen.svg                   optional snapshot of the viewer as shown
"""
import logging
import os
import shutil
from datetime import datetime

from PyQt6.QtCore import QDateTime, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
    QVBoxLayout, QWidget,
)

from src.core import export_data as ex
from src.gui.settings import gui_settings
from src.gui.style import set_role

NICE_BINS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 86400]


def auto_bin(span_s, target_bins=1500):
    want = span_s / target_bins
    for b in NICE_BINS:
        if b >= want:
            return float(b)
    return float(NICE_BINS[-1])


def _fmt_rows(n):
    return f"{n / 1e6:.1f} M" if n >= 1e6 else (f"{n / 1e3:.0f} k" if n >= 1e3 else str(int(n)))


class ExportDialog(QDialog):
    def __init__(self, parent, t0, t1, n_channels, tick_s, displayed_rows, default_dest, default_name):
        super().__init__(parent)
        self.setWindowTitle("Export data and figure")
        self.setMinimumWidth(600)
        self.view_range = (t0, t1)
        self.n_channels = max(1, n_channels)
        self.tick_s = tick_s
        self.displayed_rows = displayed_rows
        st = gui_settings("SINDIE", "DataViewer")
        lay = QVBoxLayout(self)

        # --- range
        g = QGroupBox("Time range")
        f = QFormLayout(g)
        self.dt_start, self.dt_end = QDateTimeEdit(), QDateTimeEdit()
        for w in (self.dt_start, self.dt_end):
            w.setDisplayFormat("yyyy-MM-dd HH:mm:ss.zzz")
            w.setCalendarPopup(True)
            w.dateTimeChanged.connect(self._update_estimate)
        btn_view = QPushButton("Reset to current view")
        btn_view.clicked.connect(self._reset_range)
        f.addRow("Start", self.dt_start)
        f.addRow("End", self.dt_end)
        self.lbl_span = QLabel()
        row = QHBoxLayout(); row.addWidget(self.lbl_span); row.addStretch(); row.addWidget(btn_view)
        f.addRow(row)
        lay.addWidget(g)

        # --- data resolution
        g = QGroupBox("Data")
        v = QVBoxLayout(g)
        self.rb_exact = QRadioButton("Exact: every logged sample (read from the log files)")
        self.rb_mean = QRadioButton("Time-averaged: mean per bin")
        self.rb_disp = QRadioButton("As displayed on screen (min/max envelope when zoomed out)")
        self.grp = QButtonGroup(self)
        for i, rb in enumerate((self.rb_exact, self.rb_mean, self.rb_disp)):
            self.grp.addButton(rb, i)
            v.addWidget(rb)
            if i == 1:
                h = QHBoxLayout(); h.setContentsMargins(24, 0, 0, 0)
                self.sp_bin = QDoubleSpinBox(); self.sp_bin.setDecimals(1)
                self.sp_bin.setRange(0.1, 86400); self.sp_bin.setSuffix(" s")
                self.sp_bin.valueChanged.connect(self._update_estimate)
                self.cb_spread = QCheckBox("add min / max / std columns")
                self.cb_spread.setChecked(st.value("export/spread", True, type=bool))
                h.addWidget(QLabel("Bin width")); h.addWidget(self.sp_bin); h.addWidget(self.cb_spread); h.addStretch()
                w = QWidget(); w.setLayout(h); v.addWidget(w)
        self.grp.idToggled.connect(lambda *_: self._update_estimate())
        self.grp.idToggled.connect(lambda *_: (self.sp_bin.setEnabled(self.rb_mean.isChecked()),
                                               self.cb_spread.setEnabled(self.rb_mean.isChecked())))
        self.lbl_est = QLabel(); self.lbl_est.setWordWrap(True)
        v.addWidget(self.lbl_est)
        lay.addWidget(g)

        # --- outputs
        g = QGroupBox("Files")
        v = QVBoxLayout(g)
        h = QHBoxLayout()
        self.cb_csv = QCheckBox("CSV"); self.cb_parquet = QCheckBox("Parquet")
        self.cb_markers = QCheckBox("Event markers"); self.cb_svg = QCheckBox("Screen snapshot (SVG)")
        for cb, key, d in ((self.cb_csv, "csv", True), (self.cb_parquet, "parquet", True),
                           (self.cb_markers, "markers", True), (self.cb_svg, "screen_svg", False)):
            cb.setChecked(st.value(f"export/{key}", d, type=bool)); h.addWidget(cb)
            cb.toggled.connect(self._update_estimate)
        h.addStretch(); v.addLayout(h)
        self.cb_fig = QCheckBox("Publication figure (PDF + SVG + PNG, and make_figure.py to regenerate it)")
        self.cb_fig.setChecked(st.value("export/figure", True, type=bool))
        v.addWidget(self.cb_fig)
        fl = QFormLayout(); fl.setContentsMargins(24, 0, 0, 0)
        self.sp_w = QDoubleSpinBox(); self.sp_w.setRange(5, 40); self.sp_w.setSuffix(" cm")
        self.sp_w.setValue(st.value("export/width_cm", 15.0, type=float))
        self.sp_h = QDoubleSpinBox(); self.sp_h.setRange(2, 20); self.sp_h.setSuffix(" cm")
        self.sp_h.setValue(st.value("export/lane_height_cm", 4.5, type=float))
        self.cmb_font = QComboBox(); self.cmb_font.addItems(["serif", "sans"])
        self.cmb_font.setCurrentText(st.value("export/font", "serif"))
        self.cmb_time = QComboBox(); self.cmb_time.addItems(["elapsed time", "clock time"])
        self.cmb_time.setCurrentText(st.value("export/time_axis", "elapsed time"))
        r1 = QHBoxLayout()
        r1.addWidget(self.sp_w); r1.addSpacing(16); r1.addWidget(QLabel("height per lane")); r1.addWidget(self.sp_h)
        r1.addStretch()
        r2 = QHBoxLayout()
        r2.addWidget(self.cmb_font); r2.addSpacing(16); r2.addWidget(QLabel("x axis")); r2.addWidget(self.cmb_time)
        r2.addStretch()
        fl.addRow("Width", r1)
        fl.addRow("Font", r2)
        self.fig_opts = QWidget(); self.fig_opts.setLayout(fl); v.addWidget(self.fig_opts)
        self.cb_fig.toggled.connect(self.fig_opts.setEnabled)
        self.fig_opts.setEnabled(self.cb_fig.isChecked())
        lay.addWidget(g)

        # --- destination
        g = QGroupBox("Save to")
        f = QFormLayout(g)
        self.ed_dest = QLineEdit(default_dest)      # from the viewer's Export Settings box
        b = QPushButton("..."); b.setFixedWidth(30); b.clicked.connect(self._browse)
        h = QHBoxLayout(); h.addWidget(self.ed_dest); h.addWidget(b)
        f.addRow("Folder", h)
        self.ed_name = QLineEdit(default_name)
        f.addRow("Name", self.ed_name)
        self.lbl_out = QLabel(); set_role(self.lbl_out, "caption")
        f.addRow(self.lbl_out)
        self.ed_name.textChanged.connect(self._update_estimate)
        self.ed_dest.textChanged.connect(self._update_estimate)
        lay.addWidget(g)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        mode = st.value("export/mode", "exact")
        {"exact": self.rb_exact, "mean": self.rb_mean, "displayed": self.rb_disp}.get(mode, self.rb_exact).setChecked(True)
        self._reset_range()

    # ------------------------------------------------------------------
    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Export folder", self.ed_dest.text())
        if d:
            self.ed_dest.setText(d)

    def _reset_range(self):
        t0, t1 = self.view_range
        self.dt_start.setDateTime(QDateTime.fromMSecsSinceEpoch(int(t0 * 1000)))
        self.dt_end.setDateTime(QDateTime.fromMSecsSinceEpoch(int(t1 * 1000)))
        self.sp_bin.setValue(auto_bin(t1 - t0))
        self._update_estimate()

    def _range(self):
        return self.dt_start.dateTime().toMSecsSinceEpoch() / 1000.0, self.dt_end.dateTime().toMSecsSinceEpoch() / 1000.0

    def _update_estimate(self, *_):
        t0, t1 = self._range()
        span = max(0.0, t1 - t0)
        h, rem = divmod(span, 3600)
        self.lbl_span.setText(f"Duration: {int(h)} h {int(rem // 60)} min {rem % 60:.1f} s")
        if self.rb_exact.isChecked():
            rows = span / self.tick_s
        elif self.rb_mean.isChecked():
            rows = span / max(self.sp_bin.value(), 1e-3)
        else:
            rows = self.displayed_rows
        ncol = 3 + self.n_channels * (4 if (self.rb_mean.isChecked() and self.cb_spread.isChecked()) else 1)
        csv_mb = rows * ncol * 19 / 2**20
        txt = f"About {_fmt_rows(rows)} rows x {ncol} columns"
        if self.cb_csv.isChecked():
            txt += f" (~{csv_mb:,.0f} MB as CSV)"
        if self.rb_exact.isChecked() and rows > 2e6:
            txt += (".  Large export: consider Parquet only, or time-averaging "
                    "(the figure is unaffected either way).")
            set_role(self.lbl_est, "warning")
        else:
            set_role(self.lbl_est, None)
        self.lbl_est.setText(txt)
        self.lbl_out.setText(f"-> {os.path.join(self.ed_dest.text(), self.ed_name.text() + '_<date>_<time>')}")

    def accept(self):
        t0, t1 = self._range()
        if t1 <= t0:
            self.lbl_est.setText("End must be after start.")
            set_role(self.lbl_est, "error")
            return
        o = self.options()
        st = gui_settings("SINDIE", "DataViewer")
        for k in ("mode", "spread", "csv", "parquet", "markers", "screen_svg", "figure", "width_cm",
                  "lane_height_cm", "font", "dest", "name"):
            st.setValue(f"export/{k}", o[k])
        st.setValue("export/time_axis", self.cmb_time.currentText())
        super().accept()

    def options(self):
        t0, t1 = self._range()
        mode = "exact" if self.rb_exact.isChecked() else ("mean" if self.rb_mean.isChecked() else "displayed")
        return {"t0": t0, "t1": t1, "mode": mode, "bin_s": self.sp_bin.value(),
                "spread": self.cb_spread.isChecked(), "csv": self.cb_csv.isChecked(),
                "parquet": self.cb_parquet.isChecked(), "markers": self.cb_markers.isChecked(),
                "screen_svg": self.cb_svg.isChecked(), "figure": self.cb_fig.isChecked(),
                "width_cm": self.sp_w.value(), "lane_height_cm": self.sp_h.value(),
                "font": self.cmb_font.currentText(),
                "time_axis": "clock" if self.cmb_time.currentIndex() == 1 else "elapsed",
                "dest": self.ed_dest.text(), "name": self.ed_name.text() or "SINDIE_export"}


class ExportWorker(QThread):
    """Collects data, writes files and renders the figure off the GUI thread."""
    progress = pyqtSignal(str)
    done = pyqtSignal(str)       # folder
    failed = pyqtSignal(str)

    def __init__(self, engine, live_store, project_root, options, channels, markers,
                 displayed_frame=None, screen_svg=None):
        super().__init__()
        self.engine, self.live = engine, live_store
        self.root = project_root
        self.o = options
        self.channels = channels
        self.markers = markers
        self.displayed_frame = displayed_frame
        self.screen_svg = screen_svg

    def run(self):
        try:
            o = self.o
            t0, t1 = o["t0"], o["t1"]
            tags = [c["tag"] for c in self.channels]
            folder = os.path.join(o["dest"], f"{o['name']}_{datetime.now():%Y%m%d_%H%M%S}")
            tick = ex.logger_tick_seconds(self.root)
            say = self.progress.emit
            spread = False
            if o["mode"] == "exact":
                df, info = ex.collect_exact(self.engine, self.live, t0, t1, tags, progress=say)
            elif o["mode"] == "mean":
                spread = o["spread"]
                df, info = ex.collect_mean(self.engine, self.live, t0, t1, tags, o["bin_s"], spread=spread,
                                           progress=say)
            else:
                df, info = self.displayed_frame, {"note": "as displayed"}
            if df is None or df.height == 0:
                self.failed.emit("No data in the selected range.")
                return
            for ch in self.channels:
                ch["rows_with_data"] = int(df[ch["tag"]].drop_nulls().drop_nans().len()) \
                    if ch["tag"] in df.columns else 0
            say(f"Writing {df.height:,} rows...")
            table = ex.build_table(df, self.channels, t0, spread=spread)
            meta = ex.make_metadata(t0, t1, o["mode"], self.channels, info,
                                    self.markers if o["markers"] else [], self.root,
                                    bin_s=o["bin_s"] if o["mode"] == "mean" else None, spread=spread, tick_s=tick)
            ex.write_export(folder, table, meta, self.markers if o["markers"] else [],
                            # make_figure.py reloads the data, so keep a Parquet copy with a figure
                            write_csv=o["csv"], write_parquet=o["parquet"] or o["figure"])
            ex.summary_table(df, self.channels, o["mode"]).write_csv(os.path.join(folder, "summary.csv"))
            with open(os.path.join(folder, "caption.txt"), "w", encoding="utf-8") as f:
                f.write(ex.caption_text(meta) + "\n")
            if self.screen_svg is not None:
                with open(os.path.join(folder, "screen.svg"), "wb") as f:
                    f.write(self.screen_svg)
            if o["figure"]:
                say("Rendering figure...")
                from src.gui.data_viewer import thesis_figure
                cols = {c: (table[c].to_numpy() if c != "Human_Time" else None) for c in table.columns}
                thesis_figure.render(cols, meta, os.path.join(folder, "figure"),
                                     width_cm=o["width_cm"], lane_height_cm=o["lane_height_cm"],
                                     font=o["font"], time_axis=o["time_axis"])
                shutil.copy(thesis_figure.__file__, os.path.join(folder, "make_figure.py"))
            self.done.emit(folder)
        except Exception as e:   # the dialog must hear about any failure, or it waits forever
            logging.getLogger("viewer.export").exception("Export failed")
            self.failed.emit(f"{type(e).__name__}: {e}")
