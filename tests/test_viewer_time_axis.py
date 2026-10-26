"""The data viewer's time axis: ticks on whole clock times, labelled exactly. pyqtgraph's own spacing is
decimal seconds from when the viewer opened (50 s, 100 s, 200 s, at no particular second), which the
minute labels turned into 08:48, 08:50, 08:51, 08:53 for ticks evenly 100 s apart."""
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def axis():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])          # (kept alive for the test)
    from src.gui.data_viewer.viewer_widgets import TimeAxisItem
    ax = TimeAxisItem(orientation="bottom")
    ax.setStyle(maxTickLevel=0)                                   # as the viewer has it (GRID_TICK_LEVEL)
    ax.set_offset(datetime(2026, 10, 26, 8, 41, 37, 400000).timestamp())   # opened at an odd second
    yield ax
    del app


def ticks(ax, start, end, size=1900):
    t0 = ax.offset
    lo, hi = start.timestamp() - t0, end.timestamp() - t0
    spacing, values = ax.tickValues(lo, hi, size)[0]
    return spacing, [datetime.fromtimestamp(v + t0) for v in values], ax.tickStrings(values, 1.0, spacing)


def test_minutes_on_the_minute(axis):
    day = datetime(2026, 10, 26)
    spacing, at, labels = ticks(axis, day.replace(hour=8, minute=47, second=10), day.replace(hour=8, minute=53, second=40))
    assert spacing == 60
    assert all(t.second == 0 and t.microsecond < 1000 for t in at)
    assert labels == [t.strftime("%H:%M") for t in at] == ["08:48", "08:49", "08:50", "08:51", "08:52", "08:53"]


def test_hours_and_days_on_the_local_clock(axis):
    day = datetime(2026, 10, 26)
    spacing, at, _labels = ticks(axis, day.replace(hour=7), day.replace(hour=12, minute=10))
    assert spacing in (1800, 3600) and all(t.minute % 30 == 0 and t.second == 0 for t in at)
    spacing, at, labels = ticks(axis, day, day + timedelta(days=3), size=1500)
    assert spacing >= 21600 and all(t.hour % 6 == 0 and t.minute == 0 for t in at)
    assert "00:00" in labels[[t.hour for t in at].index(0)]


def test_tenths_of_a_second(axis):
    start = datetime(2026, 10, 26, 8, 43, 17, 250000)
    spacing, at, labels = ticks(axis, start, start + timedelta(seconds=1.5))
    assert spacing == pytest.approx(0.2)
    assert all(round(t.microsecond / 1e5, 3) % 2 == 0 for t in at)
    assert labels[0] == at[0].strftime("%H:%M:%S.%f")[:-5] and len(labels[0]) == 10


def test_upper_lanes_keep_the_ticks_without_the_labels(axis):
    axis.hide_text = True
    day = datetime(2026, 10, 26)
    spacing, at, labels = ticks(axis, day.replace(hour=8, minute=47), day.replace(hour=8, minute=53))
    assert at and set(labels) == {""}
