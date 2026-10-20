"""Event markers: shown from events.db, added by double-click, deleted by right-click."""
import logging
import os
import sqlite3
import json
import pyqtgraph as pg

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer
from src.core import paths
from src.gui.data_viewer.event_overlay import event_colour
from src.gui.data_viewer.viewer_widgets import MarkerDialog


class MarkersMixin:
    """Event markers: shown from events.db, added by double-click, deleted by right-click. Mixed into DataViewerApp (viewer_main.py)."""

    def _on_graph_clicked(self, event):
        if not self.lanes: return
        ref_plot = list(self.lanes.values())[0]
        pos = event.scenePos()
        mouse_point = ref_plot.vb.mapSceneToView(pos)

        # Middle Click: Re-center
        if event.button() == Qt.MouseButton.MiddleButton:
            view_range = ref_plot.viewRange()[0]
            width = view_range[1] - view_range[0]
            ref_plot.setXRange(mouse_point.x() - width / 2, mouse_point.x() + width / 2, padding=0)
            return

        # Ctrl + Left Click: Pin
        if event.button() == Qt.MouseButton.LeftButton and QApplication.keyboardModifiers() == Qt.KeyboardModifier.ControlModifier:
            if not self.btn_inspector.isChecked(): return

            snapped_x, values = self._values_at(mouse_point.x())
            if snapped_x is None:
                return

            # Store the absolute timestamp and the exact values at the pin
            self.pin_timestamp = snapped_x + self.t0
            self.pinned_values = {tag: values.get(tag) for tag in self.cache.tags}

            # Update visual pin lines
            for pin_line in self.pin_lines:
                pin_line.setPos(self.pin_timestamp - self.t0)
                pin_line.show()

        # Right Click: Clear Pin
        elif event.button() == Qt.MouseButton.RightButton:
            self.pin_timestamp = None
            self.pinned_values = {}
            for pin_line in self.pin_lines: pin_line.hide()
            for label in self.delta_labels: label.hide()

    def _handle_click_events(self, event):
        """Double click for markers. Only triggers if clicking the background."""
        # If the user clicked an item (like an InfiniteLine), ignore this event
        if self.layout_widget.scene().items(event.scenePos()):
            for item in self.layout_widget.scene().items(event.scenePos()):
                if isinstance(item, pg.InfiniteLine):
                    return

        if event.double() and self.lanes:
            ref_plot = list(self.lanes.values())[0]
            pos = event.scenePos()
            mouse_point = ref_plot.vb.mapSceneToView(pos)
            absolute_ts = mouse_point.x() + self.t0

            dlg = MarkerDialog(self)
            if dlg.exec():
                text, color = dlg.get_data()
                if text:
                    self._save_markers(absolute_ts, {"text": text, "color": color})

    def _events_conn(self):
        """One persistent connection (needed for PRAGMA data_version change detection)."""
        db_path = paths.EVENTS_DB
        if getattr(self, '_ev_conn', None) is None:
            if not os.path.exists(db_path):
                return None
            self._ev_conn = sqlite3.connect(db_path, timeout=0.5)
        return self._ev_conn

    def _events_db_version(self):
        """Changes whenever ANOTHER connection (the events service) commits."""
        conn = self._events_conn()
        if conn is None:
            return None
        try:
            return conn.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error:
            self._ev_conn = None
            return None

    def _active_event_types(self):
        return tuple(sorted(t for label, cb in self.filter_checkboxes.items() if cb.isChecked()
                            for t in self.event_filters[label]))

    def _load_markers(self, t_start=None, t_end=None):
        """Reads filtered events from the SQLite database, optionally only for a
        time window (uses the events table's timestamp index; the old query had
        no time bound and no usable index, so it scanned every event ever logged
        by every service, every second, on the GUI thread)."""

        # 1. Determine which event types are currently checked in the UI
        active_types = []
        for label, cb in self.filter_checkboxes.items():
            if cb.isChecked():
                active_types.extend(self.event_filters[label])

        markers = {}
        if not active_types:
            return markers

        # 2. Map UI pseudo-types back to real database types
        db_query_types = []
        for t in active_types:
            if t.startswith("FAULT"):
                if "FAULT" not in db_query_types:
                    db_query_types.append("FAULT")
            else:
                db_query_types.append(t)

        placeholders = ",".join("?" * len(db_query_types))
        query = f"SELECT timestamp, event_type, message, metadata FROM events WHERE event_type IN ({placeholders})"
        params = list(db_query_types)
        if t_start is not None:
            query += " AND timestamp BETWEEN ? AND ?"
            params += [t_start, t_end]

        conn = self._events_conn()
        if conn is None:
            return markers
        try:
            cursor = conn.execute(query, params)

            for ts, e_type, msg, meta in cursor.fetchall():
                key = f"{ts:.3f}"
                kind = None
                color = ""

                if e_type == "FAULT":
                    # Extract the active state from the metadata payload
                    try:
                        is_active = json.loads(meta).get("is_active", True) if meta else True
                    except (ValueError, TypeError, AttributeError):
                        is_active = True

                    # Filter against UI checkboxes
                    if is_active and "FAULT_ACTIVE" not in active_types:
                        continue
                    if not is_active and "FAULT_CLEARED" not in active_types:
                        continue
                    kind = "fault" if is_active else "cleared"

                elif e_type == "USER_MARKER":
                    # The colour it was given (drawn adjusted for the theme: event_overlay.event_colour)
                    try:
                        color = json.loads(meta).get("color", "#00E5FF") if meta else "#00E5FF"
                    except (ValueError, TypeError, AttributeError):
                        color = "#00E5FF"

                event = {
                    "text": msg,
                    "type": e_type,
                    "meta": meta,          # shown in full in the event details window
                }
                if kind:
                    event["kind"] = kind
                # (a system event's colour: the theme's for its kind - also what an export records)
                event["color"] = color or event_colour(event).name()
                markers[key] = event
        except sqlite3.Error as e:
            logging.getLogger("viewer.markers").error("Event markers not readable from events.db: %s", e)
            self._ev_conn = None  # reconnect next time

        return markers

    def _save_markers(self, timestamp, data_dict):
        """Optimistic UI: Saves to RAM, forces checkbox ON, then publishes."""
        key = f"{timestamp:.3f}"
        data_dict = dict(data_dict, type="USER_MARKER")
        self._pending_markers[key] = data_dict

        # 1. OPTIMISTIC UI: Force the checkbox to be visible
        user_cb = self.filter_checkboxes.get("User Markers")
        if user_cb and not user_cb.isChecked():
            # Block signals temporarily to prevent a double-refresh during the save
            user_cb.blockSignals(True)
            user_cb.setChecked(True)
            user_cb.blockSignals(False)
            self._ev_types = None            # filters changed: reload on the next refresh
        self.event_data[key] = data_dict
        self._refresh_event_markers(reload=False)

        # 2. Publish to the events service, which writes it to events.db
        self.events.log_user_marker(marker_ts=timestamp, text=data_dict["text"], color=data_dict["color"])
        # Publishing can't tell whether anyone received it: check the database shortly after
        # (a marker shown but never saved also never reached exports)
        QTimer.singleShot(3000, lambda: self._verify_marker_saved(timestamp, data_dict))

    def _marker_in_db(self, timestamp):
        conn = self._events_conn()
        if conn is None:
            return False
        try:
            return conn.execute("SELECT 1 FROM events WHERE event_type = 'USER_MARKER' AND "
                                "timestamp BETWEEN ? AND ? LIMIT 1",
                                (timestamp - 0.002, timestamp + 0.002)).fetchone() is not None
        except sqlite3.Error:
            self._ev_conn = None
            return False

    def _verify_marker_saved(self, timestamp, data_dict, attempt=1):
        if self._marker_in_db(timestamp):
            self._pending_markers.pop(f"{timestamp:.3f}", None)
            return
        if attempt < 3:      # the events service writes in batches; give it a little longer
            QTimer.singleShot(2000, lambda: self._verify_marker_saved(timestamp, data_dict, attempt + 1))
            return
        key = f"{timestamp:.3f}"
        self._pending_markers.pop(key, None)
        self.event_data.pop(key, None)
        self.event_overlay.forget(key)
        self._refresh_event_markers(reload=False)
        self._marker_save_failed(data_dict.get("text", ""))

    def _marker_save_failed(self, text):
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox(QMessageBox.Icon.Warning, "Marker not saved",
                          f"The marker \"{text}\" did not reach the event database "
                          f"({paths.EVENTS_DB}), so it has been taken off the plot and would not be "
                          "in exports.\n\nIs the events service running for this system? (A viewer "
                          "started on its own - not by the service manager / run_simulation.py - "
                          "with the --sim flag mismatched, or on another PC, sends its markers "
                          "nowhere.)", parent=self)
        box.setModal(False)
        box.show()

    def _refresh_event_markers(self, *_, reload=True):
        """Synchronizes RAM markers with SQLite database and redraws the overlay.

        reload=False only re-places markers for the current view from the
        cached self.event_data (cheap). The SQLite read happens on the 1 s
        marker_sync_timer, filter changes and UI rebuilds, not on every
        render frame (it was previously hit ~11x per second).
        """
        if not self.lanes:
            self.event_overlay.clear()
            return

        base_plot = list(self.lanes.values())[0]
        view_range = base_plot.viewRange()[0]

        # Pull fresh database state - only when something actually changed:
        #   * the DB was written by another connection (PRAGMA data_version),
        #   * the overlay filters changed, or
        #   * the view left the time window loaded last time.
        # Each load is a time-windowed indexed query (view +/- 2 spans).
        span = view_range[1] - view_range[0]
        a0, a1 = view_range[0] + self.t0, view_range[1] + self.t0
        types = self._active_event_types()
        win = getattr(self, '_ev_window', None)
        need = (win is None or a0 < win[0] or a1 > win[1] or types != getattr(self, '_ev_types', None))
        if reload and not need:
            version = self._events_db_version()
            need = version != getattr(self, '_ev_version', None)
        if need:
            w0, w1 = a0 - 2 * span, a1 + 2 * span
            self._ev_version = self._events_db_version()
            self.event_data = self._load_markers(w0, w1)
            self._ev_window, self._ev_types = (w0, w1), types
            if "USER_MARKER" in types:
                self.event_data.update(self._pending_markers)   # sent, not yet in the database

        lanes = list(self.lanes.values())
        legend = self.lane_legends.get(next(iter(self.lanes)))
        self.event_overlay.update(lanes, legend, self.event_data, self.t0, view_range)

        # (A forced self.layout_widget.scene().update() used to sit here. It made Qt
        # repaint the whole scene 10x/s even when idle: ~45% CPU at 1080p. Adding or
        # removing items already schedules the repaint Qt needs.)

    def _delete_marker(self, ts_key):
        """From the marker's right-click menu (choosing 'Delete marker ...' is the confirmation)."""
        if self.event_data.get(ts_key, {}).get("type", "USER_MARKER") != "USER_MARKER":
            return      # system events are read-only (the menu doesn't offer them anyway)
        self.event_data.pop(ts_key, None)
        self._pending_markers.pop(ts_key, None)
        self.event_overlay.forget(ts_key)
        self.events.delete_user_marker(marker_ts=float(ts_key))
        self._refresh_event_markers(reload=False)
