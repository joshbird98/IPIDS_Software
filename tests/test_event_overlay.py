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
