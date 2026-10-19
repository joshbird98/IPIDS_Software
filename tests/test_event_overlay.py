"""Event overlay layout (no Qt needed): grouping by pixel distance and label rows."""
from src.gui.data_viewer.event_overlay import group_events, place_labels, shorten


def test_grouping_by_pixels():
    pts = [(0.0, "a", {}), (0.5, "b", {}), (10.0, "c", {}), (10.2, "d", {}), (10.4, "e", {})]
    groups = group_events(pts, units_per_px=0.1, gap_px=6)      # 6 px = 0.6 units
    assert [[k for _x, k, _e in g] for g in groups] == [["a", "b"], ["c", "d", "e"]]


def test_labels_take_free_rows_and_avoid_the_legend():
    spans = [(0, 100), (50, 150), (60, 160), (70, 170), (300, 400)]
    rows = place_labels(spans, n_rows=4, blocked=[(280, 420, 0)], max_levels=3)
    assert rows == [0, 1, 2, None, 1]    # 4th: only 3 levels; 5th goes below the legend in row 0


def test_shorten():
    assert shorten("short") == "short"
    s = shorten("x" * 50, 10)
    assert len(s) == 10 and s.endswith("…")


def test_labels_go_below_the_legend():
    rows = place_labels([(10, 50)], n_rows=6, blocked=[(0, 100, 0), (0, 100, 1), (0, 100, 2)], max_levels=3)
    assert rows == [3]


LONG = ("Gate valve closed by its interlock (vacuum permissive gone): its OPEN request is cleared, so it "
        "stays shut. Press OPEN again once the vacuum is OK.")


def _members():
    return [(10.0, "1000010.000", {"text": "hello world " * 4, "color": "#00E5FF", "type": "USER_MARKER",
                                   "meta": '{"color": "#00E5FF", "author": "josh"}'}),
            (10.4, "1000010.400", {"text": LONG, "color": "#F44336", "type": "FAULT",
                                   "meta": '{"is_active": true, "fault": "Vacuum_Interlock"}'})]


def test_the_details_show_every_event_in_full():
    from src.gui.data_viewer.event_overlay import details_html, group_tooltip
    page = details_html(_members(), 1_000_000.0)
    assert "Press OPEN again once the vacuum is OK." in page                # not shortened
    assert "fault: Vacuum_Interlock" in page and "author: josh" in page    # the stored details
    assert ".000</b>" in page and ".400</b>" in page                       # to the millisecond
    tip = group_tooltip(_members(), 1_000_000.0)
    assert tip.startswith("<qt>") and "vacuum is OK." in tip and "Click for the full details" in tip


def test_clicking_a_label_or_line_opens_the_details_window():
    import pyqtgraph as pg
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from src.gui.data_viewer.event_overlay import EventOverlay
    app = QApplication.instance() or QApplication([])
    win = pg.GraphicsLayoutWidget()
    plot = win.addPlot()
    plot.setXRange(0, 100)
    win.resize(800, 400)
    win.show()
    app.processEvents()
    overlay = EventOverlay(on_delete=lambda k: None)
    events = {k: e for _x, k, e in _members()}
    overlay.update([plot], None, events, 1_000_000.0, (0.0, 100.0))

    class Click:
        def __init__(self, button):
            self.b, self.accepted = button, False

        def button(self):
            return self.b

        def accept(self):
            self.accepted = True
    try:
        (dkey, item), = overlay.items.items()                  # 0.4 s apart: drawn as one group
        click = Click(Qt.MouseButton.LeftButton)
        item["lines"][0].label.mouseClickEvent(click)          # the "2 events" label
        assert click.accepted and overlay.details.isVisible()
        assert "2 events" in overlay.details.windowTitle()
        assert "Press OPEN again once the vacuum is OK." in overlay.details.text.toPlainText()
        first = overlay.details
        item["lines"][0].mouseClickEvent(Click(Qt.MouseButton.LeftButton))   # the line: same window
        assert overlay.details is first
    finally:
        if overlay.details is not None:
            overlay.details.close()
        win.close()
        app.processEvents()
