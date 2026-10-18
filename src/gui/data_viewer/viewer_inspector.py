"""The data inspector: values under the cursor, pinned points and deltas."""
import numpy as np

from PyQt6.QtCore import Qt


class InspectorMixin:
    """The data inspector: values under the cursor, pinned points and deltas. Mixed into DataViewerApp (viewer_main.py)."""

    def _values_at(self, rel_x):
        """Channel values at a time (relative x) for the inspector and pins.

        Returns (snapped_rel_x, {tag: raw value}). Uses historical data when the
        time is before the live buffer, otherwise the live window (step-hold per
        channel, since channels now have their own source timestamps).
        """
        live_start = self.cache.live.oldest_time()
        in_hist = (self.is_historical_mode and len(getattr(self, 'historical_x', [])) > 0
                   and (live_start is None or rel_x + self.t0 < live_start))
        vals = {}
        if in_hist:
            xs = self.historical_x
            idx = int(np.clip(np.searchsorted(xs, rel_x, side='right') - 1, 0, len(xs) - 1))
            for tag, y in self.historical_y.items():
                if len(y) > idx:
                    vals[tag] = float(y[idx])
            return float(xs[idx]), vals
        live = getattr(self, 'current_live', None) or {}
        if not live:
            return None, vals
        abs_ts = rel_x + self.t0
        for tag, (x, y) in live.items():
            i = np.searchsorted(x, abs_ts, side='right') - 1
            if 0 <= i < len(y):
                vals[tag] = float(y[i])
        return rel_x, vals

    def _on_mouse_moved(self, pos):
        if not self.btn_inspector.isChecked() or not self.lanes:
            return

        # 1. HOVER DETECTION
        hovered_plot = None
        min_dist = float('inf')
        for p in self.lanes.values():
            rect = p.sceneBoundingRect()
            if rect.left() <= pos.x() <= rect.right():
                dist = abs(pos.y() - rect.center().y())
                if dist < min_dist:
                    min_dist = dist
                    hovered_plot = p
        ref_plot = hovered_plot if hovered_plot else list(self.lanes.values())[0]

        mouse_point = ref_plot.vb.mapSceneToView(pos)
        actual_x, values = self._values_at(mouse_point.x())
        if actual_x is None:
            return

        for v_line in self.v_lines:
            v_line.setPos(actual_x)
            v_line.setVisible(True)

        # --- DYNAMIC ANCHORING ---
        view_rect = ref_plot.vb.viewRect()
        x_pct = (mouse_point.x() - view_rect.left()) / view_rect.width()
        y_pct = (mouse_point.y() - view_rect.bottom()) / view_rect.height()
        anchor_x = 1.1 if x_pct > 0.8 else -0.1
        anchor_y = -0.2 if y_pct > 0.5 else 1.2

        active_pin = getattr(self, 'pin_timestamp', None)
        delta_msg = ""
        if active_pin is not None:
            delta_msg = f"Δt: {self._format_delta_time(actual_x + self.t0 - active_pin)}"

        for i, p in enumerate(self.lanes.values()):
            label = self.delta_labels[i]
            if p == hovered_plot and delta_msg != "":
                label.setAnchor((anchor_x, anchor_y))
                label.setPos(actual_x, mouse_point.y())
                label.setText(delta_msg)
                label.show()
            else:
                label.hide()

        # --- SIDEBAR UPDATES --- (was inside the per-lane loop, i.e. repeated per lane)
        pinned = getattr(self, 'pinned_values', {})
        for tag, lbl in getattr(self, 'sidebar_value_labels', {}).items():
            if tag not in values:
                continue
            cfg = self.plot_config.get(tag, {})
            mult = cfg.get('multiplier', 1.0)   # same scaling as the live readout
            val = values[tag] * mult
            base_unit = getattr(self, 'system_registry', {}).get(tag, {}).get("unit", "")
            display_text = self._format_with_si_prefix(val, base_unit)
            if active_pin is not None and pinned.get(tag) is not None:
                dy = val - pinned[tag] * mult
                # Append the delta (previously this REPLACED the value text)
                display_text += f"  (\u0394: {self._format_with_si_prefix(dy, base_unit, is_delta=True)})"
            lbl.setText(display_text)

    def _ensure_inspector_items(self):
        """Syncs inspector visibility state across all lanes."""
        is_on = self.btn_inspector.isChecked()
        for v_line in self.v_lines:
            v_line.setVisible(is_on)

        has_pin = getattr(self, 'pin_timestamp', None) is not None  # (was self.pin_idx, never set)
        for pin_line in self.pin_lines:
            pin_line.setVisible(is_on and has_pin)

        for delta in self.delta_labels:
            if not (is_on and has_pin):
                delta.hide()

    def _on_inspector_toggled(self, checked):
        self._ensure_inspector_items()

        if checked:
            self.btn_inspector.setText("🔍 Inspector: ON")
            self.btn_inspector.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
            self.layout_widget.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.btn_inspector.setText("🔍 Enable Inspector")
            self.btn_inspector.setStyleSheet("")
            self.layout_widget.setCursor(Qt.CursorShape.ArrowCursor)

    def _format_delta_time(self, seconds):
        """Converts seconds into a human-readable string (e.g., 2h 46m 40s)."""
        abs_secs = abs(seconds)
        days, rem = divmod(abs_secs, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)

        parts = []
        if days > 0: parts.append(f"{int(days)}d")
        if hours > 0: parts.append(f"{int(hours)}h")
        if minutes > 0: parts.append(f"{int(minutes)}m")
        parts.append(f"{seconds:.1f}s")

        res = " ".join(parts)
        return f"-{res}" if seconds < 0 else res

    def mousePressEvent(self, event):
        if self.layout_widget.underMouse() and event.button() == Qt.MouseButton.LeftButton:
            self.layout_widget.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.btn_inspector.isChecked():
            self.layout_widget.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.layout_widget.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)
