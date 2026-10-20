"""The recipe builder's drafting: the bar where the operator asks for a draft or a review, the tab
that shows the draft's changes (to keep or not) or the review's notes, and the thread a session
runs in (src/core/recipe_ai: the model never acts on the machine - it only proposes).

    bar = DraftBar()            draft(request), review(request), cancel()
    panel = DraftPanel()        keep(), discard(), toggled(change id, kept), fix(note), go(path)
    job = Job(fn); start(job)   fn(job) runs in a thread; job.on_progress / on_done / on_failed in the GUI's
"""
import gc
import logging
import threading
import time

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
                             QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from src.core.recipe_ai.provider import AIError
from src.gui.style import set_prop, set_role, set_tone

log = logging.getLogger("hmi.recipe_builder")

PLACEHOLDER = ("Describe a recipe, or a change to this one - e.g. “after step 3, wait for the source pressure to "
               "fall below 5e-6 mbar”")
REFINE_PLACEHOLDER = "Change the draft - e.g. “make the wait 10 minutes”, or “leave the cup out of it”"
SEVERITY_TEXT = {"concern": "Concern", "suggestion": "Suggestion", "question": "Question"}


# ============================================================================================ the thread
# A run's thread hands its progress and outcome to the GUI thread through one bridge, a child of the
# application, so no Qt object belongs to a run: a QObject deleted outside its own thread can crash
# Qt, and one made for each run would be deleted wherever its last reference went - often in the
# run's thread. For the same reason the garbage collector runs only in the GUI thread while a run's
# thread is alive (_Collector).
class _Bridge(QObject):
    event = pyqtSignal(object, str, object)     # the job; "progress", "done" or "failed"; the text, outcome or AIError


_the_bridge = None


def _bridge() -> _Bridge:
    """(Made in the GUI thread, by start().)"""
    global _the_bridge
    if _the_bridge is None or sip.isdeleted(_the_bridge):
        _the_bridge = _Bridge(QCoreApplication.instance())
        _the_bridge.event.connect(_deliver)
    return _the_bridge


def _deliver(job, kind: str, payload):
    """In the GUI thread: hand a run's progress or outcome to whoever started it."""
    callback = getattr(job, "on_" + kind, None)
    if kind != "progress":
        job.on_progress = job.on_done = job.on_failed = None    # (the run is over: let go of the window)
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:           # a fault in showing a draft must never end the HMI
        log.exception("Recipe drafting: showing the %s failed", kind)


def _emit(job, kind: str, payload):
    try:
        job.bridge.event.emit(job, kind, payload)
    except RuntimeError:        # (the application has gone)
        pass


class Job:
    """One drafting or review run, plain Python (no Qt object of its own). fn(job) is called in a thread;
    it reports with job.progress(text) and stops when job.cancelled(). on_progress(text), on_done(outcome)
    and on_failed(AIError) are called in the GUI thread; set them to None to hear nothing more."""

    def __init__(self, fn, mode: str, request: str):
        self.fn = fn
        self.mode = mode                # "draft", "refine" or "review"
        self.request = request
        self.on_progress = self.on_done = self.on_failed = None
        self.bridge = None              # (set by start)
        self._cancel = threading.Event()
        self.started = time.monotonic()
        self.base = None                # the recipe as it was when the run started
        self.selected = None

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()

    def progress(self, text: str):
        _emit(self, "progress", str(text))


class _Collector:
    """While a run's thread is alive, Python's garbage collector runs in the GUI thread only, from a
    timer (as calibre does): a collection started by the run's thread would delete whatever Qt objects
    happened to be garbage, outside the GUI thread. Back to automatic once the last thread has ended."""
    INTERVAL_MS = 500

    def __init__(self):
        self.threads = []
        self.timer = None
        self.restore = False

    def add(self, thread: threading.Thread):
        if not self.threads:
            self.restore = gc.isenabled()
            gc.disable()
        self.threads.append(thread)
        if self.timer is None or sip.isdeleted(self.timer):
            self.timer = QTimer(QCoreApplication.instance())
            self.timer.setInterval(self.INTERVAL_MS)
            self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        self.threads = [t for t in self.threads if t.is_alive()]
        threshold, count = gc.get_threshold(), gc.get_count()
        for generation in range(3):         # (what the automatic collector would have done by now)
            if count[generation] <= threshold[generation]:
                break
            gc.collect(generation)
        if not self.threads:
            self.timer.stop()
            if self.restore:
                gc.enable()


_collector = _Collector()


def start(job: Job, threaded: bool = True):
    """Run the job (in a thread unless threaded is False: the tests). Its progress and its outcome, or
    an AIError, reach job.on_progress / on_done / on_failed in the GUI thread. Called in the GUI thread."""
    job.bridge = _bridge()

    def work():
        try:
            outcome = job.fn(job)
        except AIError as e:
            _emit(job, "failed", e)
        except Exception as e:      # a fault in drafting must reach the operator as a message, never end the HMI
            log.exception("Recipe drafting failed")
            _emit(job, "failed", AIError("bad_reply", f"Drafting failed: {type(e).__name__}: {e}"))
        else:
            _emit(job, "done", outcome)

    if threaded:
        thread = threading.Thread(target=work, name="recipe-draft", daemon=True)
        _collector.add(thread)
        thread.start()
    else:
        work()


# ============================================================================================ the request box
class PromptEdit(QPlainTextEdit):
    """The request: one line that grows to four. Enter asks; Shift+Enter starts a new line; Up and
    Down on the first and last line go through the earlier requests; Escape cancels."""
    submitted = pyqtSignal()
    escaped = pyqtSignal()
    MAX_LINES = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DraftRequest")
        self.setTabChangesFocus(True)
        self.setPlaceholderText(PLACEHOLDER)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._history, self._at, self._draft = [], None, ""
        self.document().contentsChanged.connect(self._fit)
        self._fit()

    def text(self) -> str:
        return self.toPlainText().strip()

    def set_text(self, text: str):
        self.setPlainText(text)
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def remember(self, text: str):
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._at = None

    def _fit(self):
        lines = max(1, min(self.MAX_LINES, int(self.document().size().height())))
        margins = self.contentsMargins()
        height = (lines * self.fontMetrics().lineSpacing() + 2 * int(self.document().documentMargin())
                  + margins.top() + margins.bottom() + 2)
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()                 # (a narrower box wraps to more lines)

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not mods & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        if key == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        cursor = self.textCursor()
        if key == Qt.Key.Key_Up and self._history and cursor.blockNumber() == 0 and not cursor.hasSelection():
            if self._at is None:
                self._at, self._draft = len(self._history), self.toPlainText()
            if self._at > 0:
                self._at -= 1
                self.set_text(self._history[self._at])
            return
        if (key == Qt.Key.Key_Down and self._at is not None and not cursor.hasSelection()
                and cursor.blockNumber() == self.document().blockCount() - 1):
            self._at += 1
            if self._at >= len(self._history):
                self._at = None
                self.set_text(self._draft)
            else:
                self.set_text(self._history[self._at])
            return
        super().keyPressEvent(event)


class DraftBar(QFrame):
    """Draft (or Refine, while a draft is shown), Review, and what the model is doing."""
    draft = pyqtSignal(str)
    review = pyqtSignal(str)
    cancel = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DraftBar")
        set_role(self, "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = PromptEdit()
        self.edit.submitted.connect(self._ask)
        self.edit.escaped.connect(lambda: self.state == "running" and self.cancel.emit())
        self.btn_draft = QPushButton("Draft")
        self.btn_draft.setObjectName("DraftButton")
        set_tone(self.btn_draft, "primary")
        self.btn_draft.setToolTip("Ask the model for a draft of what you describe (Enter). It is checked, then shown "
                                  "as changes for you to keep or discard: nothing is saved or run until you do.")
        self.btn_draft.clicked.connect(self._ask)
        self.btn_review = QPushButton("Review")
        self.btn_review.setObjectName("ReviewButton")
        self.btn_review.setToolTip("Ask the model to read the recipe against SINDIE's procedures and note what looks "
                                   "wrong or missing (what you type above, if anything, is what to look at "
                                   "especially). It changes nothing.")
        self.btn_review.clicked.connect(lambda: self.review.emit(self.edit.text()))
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("DraftCancel")
        self.btn_cancel.setToolTip("Stop waiting for the model (Escape)")
        self.btn_cancel.clicked.connect(self.cancel.emit)
        self.btn_cancel.hide()
        row.addWidget(self.edit, 1)
        for b in (self.btn_draft, self.btn_review, self.btn_cancel):
            row.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)
        self.info = QLabel()
        self.info.setObjectName("DraftInfo")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        set_role(self.info, "caption")
        layout.addWidget(self.info)
        self.state = "idle"
        self.context = ""
        self._progress = ""
        self._started = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._show_progress)
        self._unavailable = ""

    # ------------------------------------------------------------------ what the bar says
    def set_unavailable(self, reason: str):
        self._unavailable = reason
        self.set_state("unavailable")

    def set_context(self, text: str):
        """What the bar says when nothing is running (the selected step, or what drafting does)."""
        self.context = text
        if self.state in ("idle", "proposal"):
            self._say(text)

    def set_state(self, state: str, can_review: bool = True):
        """idle, running, proposal (a draft is shown: the box refines it) or unavailable."""
        self.state = state
        running = state == "running"
        self.edit.setReadOnly(running or state == "unavailable")
        self.btn_draft.setVisible(not running)
        self.btn_review.setVisible(not running)
        self.btn_cancel.setVisible(running)
        self.btn_draft.setText("Refine" if state == "proposal" else "Draft")
        self.btn_draft.setEnabled(state in ("idle", "proposal"))
        self.btn_review.setEnabled(state == "idle" and can_review)
        self.edit.setPlaceholderText(REFINE_PLACEHOLDER if state == "proposal" else PLACEHOLDER)
        if running:
            self._started = time.monotonic()
            self._progress = "Starting"
            self._timer.start()
            self._show_progress()
        else:
            self._timer.stop()
            if state == "unavailable":
                self._say(f"Drafting isn't available: {self._unavailable}", "warning")
            else:
                self._say(self.context)

    def progress(self, text: str):
        self._progress = text
        self._show_progress()

    def _show_progress(self):
        if self.state == "running":
            self._say(f"{self._progress} … {time.monotonic() - self._started:.0f} s")

    def show_error(self, text: str):
        self._say(text, "error")

    def _say(self, text: str, role: str = "caption"):
        self.info.setText(text)
        set_role(self.info, role)

    def _ask(self):
        text = self.edit.text()
        if not text or self.state not in ("idle", "proposal") or not self.btn_draft.isEnabled():
            return
        self.edit.remember(text)
        self.draft.emit(text)


# ============================================================================================ the tab
def _label(text: str, role: str = "", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if role:
        set_role(lbl, role)
    return lbl


def _bullets(items) -> str:
    return "\n".join(f"• {x}" for x in items)


class _NoteItem(QFrame):
    """A review note: where, how much it matters, what, and the fix to draft."""

    def __init__(self, note, stale: bool, on_fix, on_go):
        super().__init__()
        self.setObjectName("ReviewNote")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        head = QHBoxLayout()
        where = note.where[:1].upper() + note.where[1:] if note.where else "The recipe"
        title = _label(f"{SEVERITY_TEXT.get(note.severity, note.severity)} - {where}"
                       + (" (changed since the review)" if stale else ""), "title")
        set_role(title, "warning" if note.severity == "concern" else "title")
        head.addWidget(title, 1)
        if note.path and note.path != ("policy",) and not stale:
            go = QPushButton("Show")
            set_role(go, "link")
            go.clicked.connect(lambda: on_go(note))
            head.addWidget(go)
        self.btn_fix = None
        if note.fix and not stale:      # (a step changed since: its fix may now be about another step)
            self.btn_fix = QPushButton("Draft this fix")
            self.btn_fix.setObjectName("DraftFix")
            self.btn_fix.setToolTip(f"Draft: {note.fix}")
            self.btn_fix.clicked.connect(lambda: on_fix(note))
            head.addWidget(self.btn_fix)
        layout.addLayout(head)
        layout.addWidget(_label(note.text))
        if note.fix:
            layout.addWidget(_label(f"Fix: {note.fix}", "caption"))


class DraftPanel(QStackedWidget):
    """The Draft tab: what drafting does (idle), a draft's changes, or a review's notes."""
    keep = pyqtSignal()
    discard = pyqtSignal()
    toggled = pyqtSignal(int, bool)
    fix = pyqtSignal(object)
    go = pyqtSignal(object)
    dismiss = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DraftPanel")
        # idle
        self.idle = QWidget()
        idle = QVBoxLayout(self.idle)
        self.lbl_idle = _label("", "hint")
        idle.addWidget(self.lbl_idle)
        idle.addStretch(1)
        self.addWidget(self.idle)
        # a draft
        self.page_draft = QWidget()
        page = QHBoxLayout(self.page_draft)
        page.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        self.lbl_request = _label("", "title")
        self.lbl_summary = _label("")
        self.lbl_summary.setObjectName("DraftSummary")
        self.lbl_assumed = _label("", "caption")
        self.lbl_check = _label("", "caption")
        self.lbl_check.setObjectName("DraftValuesToCheck")
        self.lbl_cost = _label("", "hint")
        for w in (self.lbl_request, self.lbl_summary, self.lbl_assumed, self.lbl_check):
            left.addWidget(w)
        left.addStretch(1)
        left.addWidget(self.lbl_cost)
        scroll_left = QScrollArea()
        scroll_left.setWidgetResizable(True)
        scroll_left.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner.setLayout(left)
        scroll_left.setWidget(inner)
        right = QVBoxLayout()
        right.addWidget(_label("Changes - untick what you don't want; Keep makes the ticked ones", "caption"))
        self.list_changes = QListWidget()
        self.list_changes.setObjectName("DraftChanges")
        self.list_changes.setWordWrap(True)
        self.list_changes.itemChanged.connect(self._item_changed)
        right.addWidget(self.list_changes, 1)
        buttons = QHBoxLayout()
        self.btn_keep = QPushButton("Keep")
        self.btn_keep.setObjectName("DraftKeep")
        set_tone(self.btn_keep, "primary")
        self.btn_keep.clicked.connect(self.keep.emit)
        self.btn_discard = QPushButton("Discard")
        self.btn_discard.setObjectName("DraftDiscard")
        self.btn_discard.clicked.connect(self.discard.emit)
        buttons.addWidget(self.btn_keep)
        buttons.addWidget(self.btn_discard)
        buttons.addStretch(1)
        right.addLayout(buttons)
        page.addWidget(scroll_left, 2)
        page.addLayout(right, 3)
        self.addWidget(self.page_draft)
        # a review
        self.page_review = QWidget()
        review = QVBoxLayout(self.page_review)
        review.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.lbl_review = _label("", "title")
        self.lbl_review.setObjectName("ReviewSummary")
        top.addWidget(self.lbl_review, 1)
        self.btn_dismiss = QPushButton("Clear the notes")
        self.btn_dismiss.setObjectName("ReviewDismiss")
        self.btn_dismiss.clicked.connect(self.dismiss.emit)
        top.addWidget(self.btn_dismiss, 0, Qt.AlignmentFlag.AlignTop)
        review.addLayout(top)
        self.list_notes = QListWidget()
        self.list_notes.setObjectName("ReviewNotes")
        self.list_notes.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        review.addWidget(self.list_notes, 1)
        self.lbl_review_cost = _label("", "hint")
        review.addWidget(self.lbl_review_cost)
        self.addWidget(self.page_review)
        self._filling = False
        self._locked = False
        self._n_kept = 0

    def set_locked(self, locked: bool):
        """While a draft or review runs: nothing here can be changed (what is kept must be what is shown)."""
        self._locked = locked
        self.list_changes.setEnabled(not locked)
        self.btn_keep.setEnabled(not locked and self._n_kept > 0)
        self.btn_discard.setEnabled(not locked)
        self.btn_dismiss.setEnabled(not locked)
        self._lock_notes()

    def _lock_notes(self):
        for i in range(self.list_notes.count()):
            widget = self.list_notes.itemWidget(self.list_notes.item(i))
            if widget is not None and widget.btn_fix is not None:
                widget.btn_fix.setEnabled(not self._locked)

    # ------------------------------------------------------------------ idle
    def show_idle(self, text: str):
        self.lbl_idle.setText(text)
        self.setCurrentWidget(self.idle)

    # ------------------------------------------------------------------ a draft
    def show_draft(self, request: str, outcome, changes, kept: set, cost: str):
        p = outcome.proposal
        self.lbl_request.setText(f"Draft: “{request}”")
        self.lbl_summary.setText(p.summary or "(The model gave no summary.)")
        self.lbl_assumed.setText(f"It assumed:\n{_bullets(p.assumptions)}" if p.assumptions else "")
        self.lbl_assumed.setVisible(bool(p.assumptions))
        self.lbl_check.setText(f"Check these values:\n{_bullets(p.values_to_check)}" if p.values_to_check else "")
        self.lbl_check.setVisible(bool(p.values_to_check))
        self.lbl_cost.setText(f"{cost}. Nothing is saved or run until you keep it and save.")
        self._filling = True
        self.list_changes.clear()
        for c in changes:
            lines = [c.title] + [f"   − {x}" for x in c.was] + [f"   + {x}" for x in c.lines]
            if c.kind == "change":
                lines = [c.title, f"   now: {c.lines[0]}" if c.lines else "", f"   was: {c.was[0]}" if c.was else ""]
            item = QListWidgetItem("\n".join(x for x in lines if x))
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if c.id in kept else Qt.CheckState.Unchecked)
            self.list_changes.addItem(item)
        self._filling = False
        self.update_kept(kept, len(changes))
        self.setCurrentWidget(self.page_draft)

    def update_kept(self, kept: set, total: int):
        self._filling = True
        for i in range(self.list_changes.count()):
            item = self.list_changes.item(i)
            state = Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in kept else Qt.CheckState.Unchecked
            if item.checkState() != state:
                item.setCheckState(state)
        self._filling = False
        n = self._n_kept = len(kept)
        self.btn_keep.setText(keep_text(n, total))
        self.btn_keep.setEnabled(n > 0 and not self._locked)

    def _item_changed(self, item):
        if not self._filling:
            self.toggled.emit(item.data(Qt.ItemDataRole.UserRole), item.checkState() == Qt.CheckState.Checked)

    # ------------------------------------------------------------------ a review
    def show_review(self, request: str, outcome, stale: set, cost: str, on_fix, on_go):
        review = outcome.review
        focus = f" (looking at: “{request}”)" if request and request != "(review)" else ""
        n = len(review.notes)
        head = review.summary or ("No notes." if not n else "")
        self.lbl_review.setText(f"Review{focus}: {head}" if head else f"Review{focus}")
        self.list_notes.clear()
        for i, note in enumerate(review.notes):
            widget = _NoteItem(note, i in stale, on_fix, on_go)
            item = QListWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.list_notes.addItem(item)
            self.list_notes.setItemWidget(item, widget)
            item.setSizeHint(QSize(widget.sizeHint().width(), widget.sizeHint().height() + 4))
        self._lock_notes()
        self.lbl_review_cost.setText(f"{cost}. The review changes nothing: draft a fix, or edit the recipe yourself.")
        self.setCurrentWidget(self.page_review)


def keep_text(n: int, total: int) -> str:
    if total and n == total:
        return "Keep the change" if total == 1 else f"Keep all {total} changes"
    return f"Keep {n} of {total} changes"


class ProposalBanner(QFrame):
    """Above the steps while a draft is shown: how many changes, Keep and Discard."""
    keep = pyqtSignal()
    discard = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProposalBanner")
        set_role(self, "callout")
        set_prop(self, "tone", "info")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        self.label = _label("", "title")
        self.label.setObjectName("ProposalText")
        layout.addWidget(self.label, 1)
        self.btn_keep = QPushButton("Keep")
        self.btn_keep.setObjectName("BannerKeep")
        set_tone(self.btn_keep, "primary")
        self.btn_keep.clicked.connect(self.keep.emit)
        self.btn_discard = QPushButton("Discard")
        self.btn_discard.setObjectName("BannerDiscard")
        self.btn_discard.clicked.connect(self.discard.emit)
        layout.addWidget(self.btn_keep)
        layout.addWidget(self.btn_discard)
        self._locked = False
        self._n = 0
        self.hide()

    def set_locked(self, locked: bool):
        self._locked = locked
        self.btn_keep.setEnabled(self._n > 0 and not locked)
        self.btn_discard.setEnabled(not locked)

    def show_counts(self, n: int, total: int, problems: str):
        one = "its mark" if total == 1 else "a mark"
        self.label.setText(f"Draft: {total} change{'s' if total != 1 else ''}. Click {one} (+ − ~) to leave "
                           f"{'it' if total == 1 else 'one'} out.{problems}")
        self._n = n
        self.btn_keep.setText(keep_text(n, total))
        self.btn_keep.setEnabled(n > 0 and not self._locked)
        self.show()
