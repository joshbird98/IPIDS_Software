"""Event markers drawn over the viewer's lanes, kept readable at any zoom.

* Events closer together than a few pixels are drawn as one line ("12 events"), coloured by
  the most important of them (active fault > watchdog > tolerance > user marker > ...), so a
  zoomed-out view isn't a wall of lines and text.
* Labels are one line (long texts shortened) and placed in rows along the top of the first
  lane, left to right; a label that would overlap another one or the lane's legend is left
  out. Hovering over a line or label shows the full text (and the list, for a group).
* The line itself runs through every lane; only the first lane carries the label.
* Right-click a line for a menu to delete one of its user markers (system events are
  read-only). A plain click does nothing, so clicking about while zoomed out can't open a
  stream of "Delete marker?" questions any more.
"""
from datetime import datetime

import pyqtgraph as pg
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QFontMetricsF
from PyQt6.QtWidgets import QApplication, QMenu

GROUP_PX = 6            # events closer than this (pixels) are drawn as one line
ROW_PITCH_PX = 24       # label rows (a label is ~23 px high)
FIRST_ROW_PX = 13       # centre of the top row, below the top of the lane
MAX_ROWS = 6             # rows that exist; lower ones are only used to get past the legend
MAX_LEVELS = 3           # label rows used at any one place (more covers too much of the data)
LABEL_GAP_PX = 6
MAX_LABEL_CHARS = 26
TOOLTIP_LINES = 15

# Lower = more important: decides a group's colour and which text a group shows first
_PRIORITY = {"FAULT_ACTIVE": 0, "WATCHDOG_TRIP": 1, "WATCHDOG_CLEAR": 3, "TOLERANCE": 2, "USER_MARKER": 2,
             "PHASE_CHANGE": 4, "FAULT_CLEARED": 5, "COMMAND": 7}


def priority(ev: dict) -> int:
    t = ev.get("type", "")
    if t == "FAULT":
        t = "FAULT_ACTIVE" if ev.get("color", "").upper() == "#F44336" else "FAULT_CLEARED"
    return _PRIORITY.get(t, 6)


def shorten(text: str, n: int = MAX_LABEL_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def group_events(points, units_per_px, gap_px=GROUP_PX):
    """points: [(x, key, event)] sorted by x -> list of groups (each a list of points). A point
    joins the group if it is within gap_px of the group's previous point."""
    groups = []
    gap = gap_px * units_per_px
    for p in points:
        if groups and p[0] - groups[-1][-1][0] < gap:
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups


def place_labels(spans, n_rows, blocked=(), max_levels=MAX_LEVELS):
    """spans: [(left, right)] in pixels, in x order; blocked: [(left, right, row)] areas no label
    may cover (the legend); at most max_levels rows not blocked there are tried for each label.
    -> row index per span (0 = top), or None if it fits nowhere."""
    row_end = [float("-inf")] * n_rows
    out = []
    for left, right in spans:
        chosen, levels = None, 0
        for r in range(n_rows):
            if any(r == br and left < bb and right > ba for ba, bb, br in blocked):
                continue
            levels += 1
            if levels > max_levels:
                break
            if left < row_end[r] + LABEL_GAP_PX:
                continue
            chosen = r
            break
        if chosen is not None:
            row_end[chosen] = right
        out.append(chosen)
    return out


def group_text(members):
    if len(members) == 1:
        return shorten(members[0][2].get("text", ""))
    return f"{len(members)} events"


def group_tooltip(members, t0):
    lines = []
    for x, _key, ev in sorted(members, key=lambda p: p[0])[:TOOLTIP_LINES]:
        when = datetime.fromtimestamp(x + t0).strftime("%H:%M:%S")
        lines.append(f"{when}  {ev.get('text', '')}")
    if len(members) > TOOLTIP_LINES:
        lines.append(f"... and {len(members) - TOOLTIP_LINES} more (zoom in)")
    if any(ev.get("type") == "USER_MARKER" for _x, _k, ev in members):
        lines.append("(right-click to delete a user marker)")
    return "\n".join(lines)


class EventOverlay:
    def __init__(self, on_delete):
        self.on_delete = on_delete      # callback(key) for a user marker
        self.items = {}                 # display key -> {"lines": [...], "members": [...]}
        self._lanes = []
        self._t0 = 0.0

    # ------------------------------------------------------------------ bookkeeping
    def clear(self):
        for it in self.items.values():
            for line in it["lines"]:
                if line.scene():
                    line.scene().removeItem(line)
        self.items.clear()

    def forget(self, key):
        """Remove the line(s) showing marker `key` straight away (deleted / not saved)."""
        for dkey, it in list(self.items.items()):
            if any(k == key for _x, k, _e in it["members"]):
                for line in it["lines"]:
                    if line.scene():
                        line.scene().removeItem(line)
                del self.items[dkey]

    # ------------------------------------------------------------------ drawing
    def update(self, lanes, legend, events, t0, view_range):
        """lanes: PlotItems top to bottom; events: {ts_str: {"text", "color", "type"}}."""
        if not lanes:
            self.clear()
            return
        if lanes != self._lanes or t0 != self._t0:
            self.clear()
            self._lanes, self._t0 = list(lanes), t0
        base = lanes[0]
        vb = base.vb
        upp = vb.viewPixelSize()[0]
        if not upp or upp <= 0:
            return
        x0, x1 = view_range
        span = x1 - x0
        pts = sorted((float(k) - t0, k, ev) for k, ev in events.items()
                     if x0 - span <= float(k) - t0 <= x1 + span)
        groups = group_events(pts, upp)

        wanted = {}
        for g in groups:
            if len(g) == 1:
                dkey = g[0][1]
            else:
                dkey = f"group:{g[0][1]}:{g[-1][1]}:{len(g)}"
            wanted[dkey] = g

        for dkey in [k for k in self.items if k not in wanted]:
            for line in self.items[dkey]["lines"]:
                if line.scene():
                    line.scene().removeItem(line)
            del self.items[dkey]
        for dkey, g in wanted.items():
            if dkey not in self.items:
                self.items[dkey] = self._make(dkey, g, lanes)

        self._place_labels(base, legend, wanted)

    def _make(self, dkey, members, lanes):
        top = min(members, key=lambda p: priority(p[2]))
        color = top[2].get("color", "#FFFFFF")
        x = members[len(members) // 2][0] if len(members) > 1 else members[0][0]
        text = group_text(members)
        tip = group_tooltip(members, self._t0)
        lines = []
        for i, lane in enumerate(lanes):
            if i == 0:
                line = pg.InfiniteLine(pos=x, angle=90, movable=False,
                                       pen=pg.mkPen(color, width=2 if len(members) > 1 else 1.5,
                                                    style=Qt.PenStyle.DashLine),
                                       label=text, labelOpts={"position": 0.95, "color": color,
                                                              "anchors": [(0.5, 0.5), (0.5, 0.5)],   # centred: see _place_labels
                                                              "fill": (0, 0, 0, 200)})
                line.label.setToolTip(tip)
                line.label.setZValue(1000)          # above the legend if they ever meet
            else:
                line = pg.InfiniteLine(pos=x, angle=90, movable=False,
                                       pen=pg.mkPen(pg.mkColor(color).darker(130), width=1,
                                                    style=Qt.PenStyle.DashLine))
            line.setToolTip(tip)
            line.sigClicked.connect(lambda _l, ev, k=dkey: self._clicked(k, ev))
            lane.addItem(line, ignoreBounds=True)
            lines.append(line)
        return {"lines": lines, "members": members, "text": text}

    def _place_labels(self, base, legend, wanted):
        vb = base.vb
        rect = vb.sceneBoundingRect()
        if rect.height() <= 0:
            return
        n_rows = max(1, min(MAX_ROWS, int((rect.height() - FIRST_ROW_PX) // ROW_PITCH_PX)))
        fm = QFontMetricsF(QApplication.font())
        order, spans = [], []
        for dkey, g in wanted.items():
            line = self.items[dkey]["lines"][0]
            xs = vb.mapViewToScene(QPointF(line.value(), 0)).x()
            w = fm.horizontalAdvance(self.items[dkey]["text"]) + 10
            if xs + w / 2 < rect.left() or xs - w / 2 > rect.right():
                line.label.setVisible(False)
                continue
            order.append((xs, dkey))
        order.sort()
        for xs, dkey in order:
            w = fm.horizontalAdvance(self.items[dkey]["text"]) + 10
            spans.append((xs - w / 2, xs + w / 2))
        blocked = []
        if legend is not None and legend.isVisible() and getattr(legend, "items", None):
            lr = legend.sceneBoundingRect()
            for r in range(n_rows):
                yc = rect.top() + FIRST_ROW_PX + r * ROW_PITCH_PX
                if lr.top() < yc + ROW_PITCH_PX / 2 and lr.bottom() > yc - ROW_PITCH_PX / 2:
                    blocked.append((lr.left() - LABEL_GAP_PX, lr.right() + LABEL_GAP_PX, r))
        rows = place_labels(spans, n_rows, blocked)
        for (xs, dkey), row in zip(order, rows):
            label = self.items[dkey]["lines"][0].label
            if row is None:
                label.setVisible(False)
                continue
            yc = FIRST_ROW_PX + row * ROW_PITCH_PX
            label.setPosition(max(0.0, min(1.0, 1.0 - yc / rect.height())))
            label.setVisible(True)

    # ------------------------------------------------------------------ interaction
    def _clicked(self, dkey, ev):
        if ev.button() != Qt.MouseButton.RightButton:
            return                      # plain clicks pass through to the plot
        ev.accept()                     # one menu, however many lines are under the cursor
        it = self.items.get(dkey)
        if it is None:
            return
        menu = QMenu()
        for x, key, e in sorted(it["members"], key=lambda p: p[0])[:25]:
            when = datetime.fromtimestamp(x + self._t0).strftime("%H:%M:%S")
            if e.get("type") == "USER_MARKER":
                act = menu.addAction(f"Delete marker {when}  '{shorten(e.get('text', ''), 40)}'")
                act.triggered.connect(lambda _c=False, k=key: self.on_delete(k))
            else:
                act = menu.addAction(f"{when}  {shorten(e.get('text', ''), 40)}  (system event, read-only)")
                act.setEnabled(False)
        if len(it["members"]) > 25:
            menu.addAction(f"... {len(it['members']) - 25} more - zoom in").setEnabled(False)
        menu.exec(ev.screenPos().toPoint())
