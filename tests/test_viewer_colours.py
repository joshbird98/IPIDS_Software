"""The data viewer's colours: a channel keeps its line colour while it stays in its lane (adding or
removing another channel doesn't repaint it), more than eight in one lane are told apart by dashes,
and event markers are coloured by what they are, in the current theme."""
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Lanes:
    """Just the colour bookkeeping of the viewer's lanes (viewer_lanes.LanesMixin)."""

    def __init__(self, config):
        from src.gui.data_viewer.viewer_lanes import LanesMixin
        self.plot_config = config
        self.mixin = LanesMixin

    def assign(self):
        self.mixin._assign_series_slots(self)

    def slot(self, tag):
        return self._series_slots[tag][1]

    def pen(self, tag):
        return self.mixin._series_pen(self, tag)


def cfg(*tags, lane="Lane 1"):
    return {t: {"selected": True, "lane": lane} for t in tags}


def test_a_channel_keeps_its_colour_when_another_is_added_or_removed(app):
    lanes = Lanes(cfg("b.pressure", "d.pressure"))
    lanes.assign()
    before = {t: lanes.slot(t) for t in ("b.pressure", "d.pressure")}
    lanes.plot_config.update(cfg("a.pressure"))           # sorts first: must not take slot 0 from b
    lanes.assign()
    assert {t: lanes.slot(t) for t in before} == before
    assert lanes.slot("a.pressure") == 2
    del lanes.plot_config["b.pressure"]
    lanes.assign()
    assert lanes.slot("d.pressure") == before["d.pressure"]


def test_colours_are_per_lane(app):
    lanes = Lanes({**cfg("x.v", "y.v", lane="Lane 1"), **cfg("z.i", lane="Lane 2")})
    lanes.assign()
    assert lanes.slot("z.i") == 0                          # its lane's first colour
    lanes.plot_config["y.v"]["lane"] = "Lane 2"           # moved: takes a free slot in the new lane
    lanes.assign()
    assert lanes.slot("y.v") == 1 and lanes.slot("z.i") == 0


def test_a_ninth_line_in_a_lane_is_dashed(app):
    lanes = Lanes(cfg(*[f"t{i:02d}.v" for i in range(10)]))
    lanes.assign()
    first, ninth = lanes.pen("t00.v"), lanes.pen("t08.v")
    assert first.color() == ninth.color()
    assert first.style() == Qt.PenStyle.SolidLine and ninth.style() == Qt.PenStyle.DashLine


def test_event_kinds_and_their_colours(app):
    from src.gui import theme
    from src.gui.data_viewer.event_overlay import event_colour, kind_of, priority
    assert kind_of({"type": "FAULT", "meta": '{"is_active": true}'}) == "fault"
    assert kind_of({"type": "FAULT", "meta": '{"is_active": false}'}) == "cleared"
    assert kind_of({"type": "FAULT", "color": "#F44336"}) == "fault"      # read before "kind" existed
    assert kind_of({"type": "WATCHDOG_CLEAR"}) == "watchdog_clear"
    assert kind_of({"type": "USER_MARKER", "color": "#00E5FF"}) == "marker"
    assert priority({"type": "FAULT", "kind": "fault"}) < priority({"type": "USER_MARKER"}) \
        < priority({"type": "FAULT", "kind": "cleared"})
    theme.apply_theme(app, "light")
    try:
        assert event_colour({"type": "FAULT", "kind": "fault"}) == theme.color("ev_fault")
        white = event_colour({"type": "USER_MARKER", "color": "#FFFFFF"})       # on the white plot
        assert theme._contrast(white, theme.qcolor(theme.tokens()["plot_bg"])) >= 3.0
    finally:
        theme.apply_theme(app, theme.DEFAULT_MODE)
