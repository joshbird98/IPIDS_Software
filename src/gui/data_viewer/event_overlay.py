"""Event markers drawn over the viewer's lanes, kept readable at any zoom.

* Events closer together than a few pixels are drawn as one line ("12 events"), coloured by
  the most important of them (active fault > watchdog > tolerance > user marker > ...), so a
  zoomed-out view isn't a wall of lines and text.
* Labels are one line (long texts shortened) and placed in rows along the top of the first
  lane, left to right; a label that would overlap another one or the lane's legend is left
  out. Hovering over a line or label shows the texts in full (wrapped), for a group the first
  few.
* Click a label or a line to read everything: an event details window lists each event of it
  (or of the group) with its date and time to the millisecond, type, full text and the details
  stored with it. It is one window, reused - clicking another line shows that one instead -
  and its text can be selected and copied. (It used to be the hover text only, cut at 15 lines,
  and hovering a 1-2 px line is hit and miss.)
* The line itself runs through every lane; only the first lane carries the label.
* Right-click a line for a menu to delete one of its user markers (system events are
  read-only), or open the details. Deleting is only ever from that menu, so clicking about
  while zoomed out can't open a stream of "Delete marker?" questions.
* Colours come from the theme by the kind of event (src/gui/theme.py: ev_fault, ev_cleared...);
  a user marker keeps the colour it was given, made darker or lighter if needed to stand out on
  the plot background (a white marker on the light theme's white plot).
"""
import html
import json
from datetime import datetime

import pyqtgraph as pg
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QFontMetricsF
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMenu, QTextBrowser, QVBoxLayout

from src.gui import theme

GROUP_PX = 6            # events closer than this (pixels) are drawn as one line
ROW_PITCH_PX = 24       # label rows (a label is ~23 px high)
FIRST_ROW_PX = 13       # centre of the top row, below the top of the lane
MAX_ROWS = 6             # rows that exist; lower ones are only used to get past the legend
MAX_LEVELS = 3           # label rows used at any one place (more covers too much of the data)
LABEL_GAP_PX = 6
MAX_LABEL_CHARS = 26
TOOLTIP_LINES = 15

# Lower = more important: decides a group's colour and which text a group shows first
_PRIORITY = {"fault": 0, "watchdog": 1, "watchdog_clear": 3, "tolerance": 2, "marker": 2,
             "phase": 4, "cleared": 5, "command": 7}
# Theme colour of each kind (a user marker has its own)
_COLOUR = {"fault": "ev_fault", "cleared": "ev_cleared", "watchdog": "ev_watchdog", "watchdog_clear": "ev_watchdog",
           "phase": "ev_phase", "tolerance": "ev_tolerance", "command": "ev_command"}
LEGACY_FAULT_RED = "#F44336"     # (events read before "kind" existed carried this for an active fault)


def kind_of(ev: dict) -> str:
    """What an event is: fault (activated), cleared, watchdog, watchdog_clear, tolerance, marker,
    phase, command - or other."""
    if ev.get("kind"):
        return ev["kind"]
    t = ev.get("type", "")
    if t == "FAULT":
        active = None
        meta = ev.get("meta")
        if meta:
            try:
                active = json.loads(meta).get("is_active") if isinstance(meta, str) else meta.get("is_active")
            except (ValueError, TypeError, AttributeError):
                active = None
        if active is None:
            active = str(ev.get("color", "")).upper() == LEGACY_FAULT_RED
        return "fault" if active else "cleared"
    if t == "USER_MARKER":
        return "marker"
    if t.startswith("WATCHDOG"):
        return "watchdog_clear" if "CLEAR" in t else "watchdog"
    return {"PHASE_CHANGE": "phase", "TOLERANCE": "tolerance", "COMMAND": "command"}.get(t, "other")


def priority(ev: dict) -> int:
    return _PRIORITY.get(kind_of(ev), 6)


def event_colour(ev: dict):
    """The colour an event is drawn in, in the current theme (a QColor)."""
    kind = kind_of(ev)
    if kind in _COLOUR:
        return theme.color(_COLOUR[kind])
    return theme.readable(ev.get("color") or theme.tokens()["ev_marker"])


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
    """Rich text, so Qt wraps long texts instead of running them off the screen."""
    lines = []
    for x, _key, ev in sorted(members, key=lambda p: p[0])[:TOOLTIP_LINES]:
        when = datetime.fromtimestamp(x + t0).strftime("%H:%M:%S")
        lines.append(f"<b>{when}</b>&nbsp; {html.escape(str(ev.get('text', '')))}")
    if len(members) > TOOLTIP_LINES:
        lines.append(f"... and {len(members) - TOOLTIP_LINES} more")
    hint = "Click for the full details"
    if any(ev.get("type") == "USER_MARKER" for _x, _k, ev in members):
        hint += "; right-click to delete a user marker"
    lines.append(f"<i>{hint}.</i>")
    return "<qt>" + "<br>".join(lines) + "</qt>"


def _metadata_text(meta) -> str:
    if meta in (None, "", {}):
        return ""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return meta
    if isinstance(meta, dict):
        return "\n".join(f"{k}: {v if not isinstance(v, (dict, list)) else json.dumps(v)}" for k, v in meta.items())
    return json.dumps(meta)


def details_html(members, t0) -> str:
    """Every event of a line or group, nothing shortened: date and time (ms), type, full text and
    the details stored with it."""
    parts = []
    for x, _key, ev in sorted(members, key=lambda p: p[0]):
        when = datetime.fromtimestamp(x + t0)
        stamp = when.strftime("%Y-%m-%d %H:%M:%S.") + f"{when.microsecond // 1000:03d}"
        color = event_colour(ev).name()
        kind = html.escape(str(ev.get("type", "")))
        text = html.escape(str(ev.get("text", ""))).replace("\n", "<br>")
        meta = _metadata_text(ev.get("meta"))
        block = (f"<p><span style='color:{color}'>&#9632;</span> <b>{stamp}</b> &nbsp;<i>{kind}</i><br>"
                 f"{text}")
        if meta:
            block += (f"<br><span style='color:{theme.tokens()['text_muted']}; font-family:monospace'>"
                      + html.escape(meta).replace("\n", "<br>") + "</span>")
        parts.append(block + "</p>")
    return "<hr>".join(parts)


class EventDetails(QDialog):
    """The events of one line (or group), in full; selectable and copyable. One window, reused."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Event details")
        self.setModal(False)
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        lay.addWidget(self.text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        lay.addWidget(buttons)

    def show_events(self, members, t0):
        n = len(members)
        self.setWindowTitle("Event details" if n == 1 else f"Event details - {n} events")
        self.text.setHtml(details_html(members, t0))
        self.show()
        self.raise_()
        self.activateWindow()


class EventOverlay:
    def __init__(self, on_delete):
        self.on_delete = on_delete      # callback(key) for a user marker
        self.items = {}                 # display key -> {"lines": [...], "members": [...]}
        self._lanes = []
        self._t0 = 0.0
        self.details = None             # the EventDetails window, made on the first click

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
        color = event_colour(top[2])
        ink = theme.readable(color, minimum=4.5)          # the label's text: readable as text
        x = members[len(members) // 2][0] if len(members) > 1 else members[0][0]
        text = group_text(members)
        tip = group_tooltip(members, self._t0)
        lines = []
        for i, lane in enumerate(lanes):
            if i == 0:
                line = pg.InfiniteLine(pos=x, angle=90, movable=False,
                                       pen=pg.mkPen(color, width=2 if len(members) > 1 else 1.5,
                                                    style=Qt.PenStyle.DashLine),
                                       label=text, labelOpts={"position": 0.95, "color": ink,
                                                              "anchors": [(0.5, 0.5), (0.5, 0.5)],   # centred: see _place_labels
                                                              "fill": theme.color("plot_legend_bg")})
                line.label.setToolTip(tip)
                line.label.setZValue(1000)          # above the legend if they ever meet
                # (a fixed label ignores clicks; this makes clicking it show the details)
                line.label.mouseClickEvent = lambda ev, k=dkey: self._clicked(k, ev)
            else:
                faint = pg.mkColor(color)
                faint.setAlpha(150)             # the same line, quieter, through the other lanes
                line = pg.InfiniteLine(pos=x, angle=90, movable=False,
                                       pen=pg.mkPen(faint, width=1, style=Qt.PenStyle.DashLine))
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
            if xs < rect.left() or xs > rect.right():           # (its line isn't in view)
                line.label.setVisible(False)
                continue
            order.append((xs, dkey))
        order.sort()
        sides = []                  # where a label sits on its line: centred, or beside it near an edge
        for xs, dkey in order:
            w = fm.horizontalAdvance(self.items[dkey]["text"]) + 10
            if xs - w / 2 < rect.left():                        # (centred, it would be cut off)
                sides.append(0.0)
                spans.append((xs, xs + w))
            elif xs + w / 2 > rect.right():
                sides.append(1.0)
                spans.append((xs - w, xs))
            else:
                sides.append(0.5)
                spans.append((xs - w / 2, xs + w / 2))
        blocked = []
        if legend is not None and legend.isVisible() and getattr(legend, "items", None):
            lr = legend.sceneBoundingRect()
            for r in range(n_rows):
                yc = rect.top() + FIRST_ROW_PX + r * ROW_PITCH_PX
                if lr.top() < yc + ROW_PITCH_PX / 2 and lr.bottom() > yc - ROW_PITCH_PX / 2:
                    blocked.append((lr.left() - LABEL_GAP_PX, lr.right() + LABEL_GAP_PX, r))
        rows = place_labels(spans, n_rows, blocked)
        for (xs, dkey), row, side in zip(order, rows, sides):
            label = self.items[dkey]["lines"][0].label
            if row is None:
                label.setVisible(False)
                continue
            label.anchors = [(side, 0.5), (side, 0.5)]
            yc = FIRST_ROW_PX + row * ROW_PITCH_PX
            label.setPosition(max(0.0, min(1.0, 1.0 - yc / rect.height())))     # (applies the anchor too)
            label.setVisible(True)

    # ------------------------------------------------------------------ interaction
    def show_details(self, dkey):
        it = self.items.get(dkey)
        if it is None:
            return
        if self.details is None:
            views = self._lanes[0].scene().views() if self._lanes and self._lanes[0].scene() else []
            self.details = EventDetails(views[0].window() if views else None)
        self.details.show_events(it["members"], self._t0)

    def _clicked(self, dkey, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            ev.accept()                 # read it all (one window, reused: no pile of them)
            self.show_details(dkey)
            return
        if ev.button() != Qt.MouseButton.RightButton:
            return
        ev.accept()                     # one menu, however many lines are under the cursor
        it = self.items.get(dkey)
        if it is None:
            return
        menu = QMenu()
        menu.addAction("Show the full details...").triggered.connect(lambda _c=False: self.show_details(dkey))
        menu.addSeparator()
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
