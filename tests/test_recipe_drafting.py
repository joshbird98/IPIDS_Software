"""Drafting in the recipe builder (src/gui/control/dialogs/recipe_builder/drafting.py and dialog.py),
with a scripted model: a draft shown as changes in the list of steps, some kept and some not, one
undo for all of them, a refinement, a review's notes beside the steps and a fix drafted from one,
a failure, a cancel, and the builder locked while a draft runs or is shown."""
import json
import time

import pytest

from src.core.recipe_ai.provider import AIError, Call, Reply

CUP = "ion_beam.beamline.diagnostics.cmd_insert_fc"


@pytest.fixture
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def folder(tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "waits.json").write_text(json.dumps({"recipe_name": "Waits", "version": "1.0", "steps": [
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1, "comment": "one"},
        {"step_id": 2, "type": "WAIT_TIME", "duration_sec": 2, "comment": "two"},
        {"step_id": 3, "type": "WAIT_TIME", "duration_sec": 3, "comment": "three"}]}))
    return recipes


class Model:
    """The scripted model: each session takes the next script (a list of replies or exceptions)."""

    def __init__(self):
        self.scripts, self.sessions = [], []

    def provider(self, settings, progress=None, cancelled=None, http_client=None):
        model = self

        class Chat:
            model = "fake-model"

            def __init__(self, script):
                self.script, self.sent = script, []

            def send(self, text=None, results=None):
                self.sent.append((text, results))
                item = self.script.pop(0)
                if callable(item):
                    item = item()
                if isinstance(item, Exception):
                    raise item
                return item

        class Provider:
            def start(self, system, tools):
                chat = Chat(model.scripts.pop(0))
                model.sessions.append(chat)
                return chat
        return Provider()


def propose(data, summary="Done."):
    return Reply(calls=[Call("propose_recipe", {"recipe_json": json.dumps(data), "summary": summary,
                                                "values_to_check": ["4 s"]}, "p")], usage={"input": 1500})


@pytest.fixture
def make(app, folder, monkeypatch):
    from src.core.recipe_ai import agent
    from src.gui.control.dialogs.recipe_builder import dialog as D
    model = Model()
    monkeypatch.setattr(agent, "make_provider", model.provider)
    monkeypatch.setattr(D, "availability", lambda settings=None: (True, "Fake, fake-model"))
    monkeypatch.setattr(D.RecipeBuilderDialog, "_tell", lambda self, *a, **k: None)
    monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_unsaved", lambda self: "discard")
    monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_save_details", lambda self, *a: ("2.0", "test"))
    made = []

    def build(load="waits.json", threaded=False):
        dlg = D.RecipeBuilderDialog(None, str(folder), load_filepath=str(folder / load) if load else None)
        dlg.ai_threaded = threaded
        dlg.model = model
        made.append(dlg)
        return dlg

    yield build
    for dlg in made:
        try:
            dlg._closing_ok = True
            dlg.close()
        except RuntimeError:
            pass
    app.processEvents()


def changed(dlg, edit):
    data = json.loads(json.dumps(dlg.data))
    edit(data)
    for s in data["steps"]:
        s.pop("step_id", None)
    return data


def rows(dlg):
    from src.gui.control.dialogs.recipe_builder.outline import CHANGE, KEPT, NUMBER
    out = []
    for i in range(dlg.outline.topLevelItemCount()):
        item = dlg.outline.topLevelItem(i)
        out.append((item.data(0, NUMBER), item.data(0, CHANGE) or "", item.data(0, KEPT)))
    return out


def test_a_draft_is_shown_as_changes_and_kept_in_part(make):
    dlg = make()
    proposal = changed(dlg, lambda d: (d["steps"].__setitem__(1, {"type": "WAIT_TIME", "duration_sec": 4,
                                                                  "comment": "two"}),
                                       d["steps"].append({"type": "WAIT_TIME", "duration_sec": 9, "comment": "nine"})))
    dlg.model.scripts.append([propose(proposal, "Step 2 to 4 s, and a 9 s wait.")])
    dlg.outline.select_path((1,), quiet=False)
    dlg.request_draft("make step 2 four seconds and add a 9 s wait at the end")
    sent = dlg.model.sessions[0].sent[0][0]
    assert "make step 2 four seconds" in sent and "Selected in the list: step 2" in sent
    assert dlg._busy() and dlg._draft is not None
    assert rows(dlg) == [("1", "", True), ("2", "change", True), ("3", "", True), ("4", "add", True)]
    assert not dlg.banner.isHidden() and dlg.banner.btn_keep.text() == "Keep all 2 changes"
    assert dlg.lbl_state.text().startswith("With the draft:")
    assert dlg.draft_panel.lbl_summary.text() == "Step 2 to 4 s, and a 9 s wait."
    assert dlg.data["steps"][1]["duration_sec"] == 2                     # nothing kept yet
    # locked while a draft is shown
    dlg.add_block("wait_time")
    dlg.delete_step()
    assert len(dlg.data["steps"]) == 3 and not dlg.save(version="2.0") and not dlg.btn_save.isEnabled()
    # leave the added wait out: by its mark, as in the list
    add = next(c for c in dlg._draft.changes if c.kind == "add")
    dlg.outline.change_clicked.emit(add.id)
    assert rows(dlg)[-1] == ("", "add", False) and dlg.banner.btn_keep.text() == "Keep 1 of 2 changes"
    assert dlg.draft_panel.list_changes.item(1).checkState().name == "Unchecked"
    dlg.keep_draft()
    assert not dlg._busy() and [s["duration_sec"] for s in dlg.data["steps"]] == [1, 4, 3]
    assert dlg.is_dirty() and "Kept 1 of 2 changes" in dlg.lbl_status.text()
    dlg.undo()                                                           # one undo: the draft's changes
    assert [s["duration_sec"] for s in dlg.data["steps"]] == [1, 2, 3]


def test_a_draft_discarded_leaves_the_recipe_and_a_refinement_carries_on(make):
    dlg = make()
    first = changed(dlg, lambda d: d["steps"][0].__setitem__("duration_sec", 10))
    second = changed(dlg, lambda d: d["steps"][0].__setitem__("duration_sec", 20))
    dlg.model.scripts.append([propose(first), propose(second, "20 s now.")])
    dlg.request_draft("make step 1 longer")
    assert dlg.draft_bar.btn_draft.text() == "Refine"
    dlg.request_draft("no, 20 s")
    chat = dlg.model.sessions[0]
    assert len(dlg.model.sessions) == 1 and "no, 20 s" in chat.sent[-1][0] and chat.sent[-1][1]
    assert dlg._draft.changes[0].lines[0] == "Wait 20 s"
    dlg.discard_draft()
    assert not dlg._busy() and dlg.data["steps"][0]["duration_sec"] == 1 and not dlg.is_dirty()
    assert dlg.draft_bar.btn_draft.text() == "Draft"


def test_a_draft_that_changes_nothing_says_so(make):
    dlg = make()
    dlg.model.scripts.append([propose(changed(dlg, lambda d: None), "A recipe can't stop the turbo pump.")])
    dlg.request_draft("stop the turbo pump at the end")
    assert dlg._draft is None and not dlg._busy()
    assert "changes nothing" in dlg.lbl_status.text() and "turbo pump" in dlg.lbl_status.text()


def test_a_failure_is_shown_and_the_builder_unlocked(make):
    dlg = make()
    dlg.model.scripts.append([AIError("quota", "The service's rate limit is used up: try again in a minute.")])
    dlg.request_draft("anything")
    assert not dlg._busy() and "rate limit" in dlg.draft_bar.info.text() and dlg.btn_save.isEnabled()


def test_a_review_leaves_notes_and_a_fix_can_be_drafted(make):
    from src.gui.control.dialogs.recipe_builder.outline import REVIEW
    dlg = make()
    dlg.model.scripts.append([Reply(calls=[Call("submit_review", {"summary": "Two things.", "notes": [
        {"step": "2", "severity": "concern", "text": "2 s is short.", "fix": "Make step 2 five seconds"},
        {"step": "stop conditions", "severity": "suggestion", "text": "No stop conditions."}]}, "r")])])
    dlg.request_review("")
    assert not dlg._busy() and dlg.outline.topLevelItem(1).data(0, REVIEW) == "2 s is short."
    assert dlg.tabs.tabText(dlg.tabs.currentIndex()) == "Review (2 notes)"
    assert dlg.draft_panel.list_notes.count() == 2
    note = dlg._review[0].review.notes[0]
    fixed = changed(dlg, lambda d: d["steps"][1].__setitem__("duration_sec", 5))
    dlg.model.scripts.append([propose(fixed)])
    dlg.draft_fix(note)
    assert "Make step 2 five seconds" in dlg.model.sessions[-1].sent[0][0]
    assert "Selected in the list: step 2" in dlg.model.sessions[-1].sent[0][0]
    dlg.keep_draft()
    # the note's step has changed since: no longer shown beside it, and marked in the tab (shown again)
    assert dlg.outline.topLevelItem(1).data(0, REVIEW) in ("", None)
    assert 0 in dlg._review_stale and dlg.draft_panel.currentWidget() is dlg.draft_panel.page_review
    assert dlg.tabs.tabText(dlg.tabs.indexOf(dlg.draft_panel)) == "Review (2 notes)"
    fix = dlg.draft_panel.list_notes.itemWidget(dlg.draft_panel.list_notes.item(0)).btn_fix
    assert fix is None                                  # (its step has changed: the fix may be about another)
    dlg.clear_review()
    assert dlg._review is None and dlg.tabs.tabText(dlg.tabs.indexOf(dlg.draft_panel)) == "Draft"


def test_cancel_drops_a_late_answer(make, app):
    dlg = make(threaded=True)
    released, answered = [], []

    def slow():
        while not released:
            time.sleep(0.01)
        answered.append(1)
        return propose(changed(dlg, lambda d: d["steps"][0].__setitem__("duration_sec", 30)))
    dlg.model.scripts.append([slow])
    dlg.request_draft("longer")
    assert dlg._job is not None and not dlg.btn_save.isEnabled() and dlg.draft_bar.btn_cancel.isVisibleTo(dlg)
    deadline = time.monotonic() + 5
    while not dlg.model.sessions and time.monotonic() < deadline:
        time.sleep(0.01)                            # (the model is asked: the thread waits in it)
    dlg.reject()                                    # Escape: cancels the draft, doesn't close the window
    assert dlg._job is None and dlg._closing_ok is False and dlg.btn_save.isEnabled()
    released.append(1)
    deadline = time.monotonic() + 5
    while not answered and time.monotonic() < deadline:
        time.sleep(0.01)
    for _ in range(50):
        app.processEvents()
        time.sleep(0.005)
    assert answered and dlg._draft is None and dlg.data["steps"][0]["duration_sec"] == 1 and not dlg._busy()


def test_a_threaded_draft_reports_progress_and_arrives(make, app):
    dlg = make(threaded=True)
    dlg.model.scripts.append([Reply(calls=[Call("find_tags", {"query": "faraday cup"}, "a")]),
                              propose(changed(dlg, lambda d: d["steps"].append(
                                  {"type": "ACTION", "target": CUP, "value": True})))])
    dlg.request_draft("put the cup in at the end")
    deadline = time.monotonic() + 10
    while dlg._job is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert dlg._draft is not None and dlg._draft.changes[0].kind == "add"


def test_closing_with_a_draft_shown_asks(make, monkeypatch):
    from src.gui.control.dialogs.recipe_builder import dialog as D
    dlg = make()
    dlg.model.scripts.append([propose(changed(dlg, lambda d: d["steps"].pop()))])
    dlg.request_draft("drop the last step")
    monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_pending_draft", lambda self: "cancel")
    assert not dlg._may_discard() and dlg._draft is not None
    monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_pending_draft", lambda self: "keep")
    monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_unsaved", lambda self: "cancel")
    assert not dlg._may_discard() and dlg._draft is None and len(dlg.data["steps"]) == 2


def test_without_a_key_the_bar_says_why(app, folder, monkeypatch):
    from src.gui.control.dialogs.recipe_builder import dialog as D
    monkeypatch.setattr(D, "availability", lambda settings=None: (False, "No API key for Gemini: add it."))
    dlg = D.RecipeBuilderDialog(None, str(folder))
    try:
        assert dlg.draft_bar.state == "unavailable" and "No API key" in dlg.draft_bar.info.text()
        assert not dlg.draft_bar.btn_draft.isEnabled()
        dlg.request_draft("anything")
        assert dlg._job is None
    finally:
        dlg._closing_ok = True
        dlg.close()


def test_drafting_switched_off_hides_it(app, folder, monkeypatch):
    from src.core.recipe_ai.settings import Settings
    from src.gui.control.dialogs.recipe_builder import dialog as D
    monkeypatch.setattr(D.Settings, "load", classmethod(lambda cls, path=None: Settings(enabled=False)))
    dlg = D.RecipeBuilderDialog(None, str(folder))
    try:
        assert dlg.draft_bar.isHidden() and not dlg.tabs.isTabVisible(dlg.tabs.indexOf(dlg.draft_panel))
    finally:
        dlg._closing_ok = True
        dlg.close()


def test_a_run_hands_back_in_the_gui_thread_through_one_bridge(app):
    """No Qt object belongs to a run (one deleted in the run's thread can crash Qt), and the garbage
    collector runs only in the GUI thread while a run's thread is alive."""
    import gc
    import threading
    from src.gui.control.dialogs.recipe_builder import drafting
    gui = threading.get_ident()
    seen, bridges = [], []

    def fn(job):
        assert threading.get_ident() != gui
        job.progress("looking")
        return "outcome"

    for run in range(2):
        job = drafting.Job(fn, "draft", "x")
        job.on_progress = lambda text: seen.append(("progress", text, threading.get_ident() == gui))
        job.on_done = lambda outcome: seen.append(("done", outcome, threading.get_ident() == gui))
        drafting.start(job, threaded=True)
        assert not gc.isenabled()
        deadline = time.monotonic() + 5
        while sum(s[0] == "done" for s in seen) <= run and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        bridges.append(job.bridge)
        assert job.on_done is None              # (let go of once the run is over)
    assert seen == [("progress", "looking", True), ("done", "outcome", True)] * 2
    assert bridges[0] is bridges[1]
    for _ in range(100):                        # (the threads end just after they hand back)
        if not any(t.is_alive() for t in drafting._collector.threads):
            break
        time.sleep(0.01)
    drafting._collector.tick()
    assert gc.isenabled() and not drafting._collector.timer.isActive()


def test_a_fault_in_showing_a_result_is_logged_not_raised(app, caplog):
    from src.gui.control.dialogs.recipe_builder import drafting
    job = drafting.Job(lambda j: "outcome", "draft", "x")
    job.on_done = lambda outcome: 1 / 0
    drafting.start(job, threaded=False)
    assert "showing the done failed" in caplog.text and job.on_done is None



# ------------------------------------------------------------------------------------------------ found in review
TRIP = {"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_0_system", "bit_str": "0.1",
        "name": "Safety_Relay_Tripped", "action": "ABORT"}


def test_a_refinement_running_locks_the_draft_shown(make):
    """The Draft tab's ticks and buttons stayed live while a refinement ran, and a change unticked
    then was kept anyway."""
    from PyQt6.QtCore import Qt
    dlg = make()
    two = changed(dlg, lambda d: (d["steps"][0].__setitem__("duration_sec", 10),
                                  d["steps"][2].__setitem__("duration_sec", 30)))
    released = []

    def slow():
        while not released:
            time.sleep(0.01)
        return propose(two)
    dlg.model.scripts.append([propose(two), slow])
    dlg.request_draft("longer")
    assert len(dlg._draft.changes) == 2
    dlg.ai_threaded = True
    try:
        dlg.request_draft("and the middle one")        # a refinement: in its thread, waiting for the model
        panel = dlg.draft_panel
        assert dlg._job is not None and not panel.list_changes.isEnabled()
        assert not panel.btn_keep.isEnabled() and not panel.btn_discard.isEnabled()
        assert not dlg.banner.btn_keep.isEnabled() and not dlg.banner.btn_discard.isEnabled()
        panel.list_changes.item(1).setCheckState(Qt.CheckState.Unchecked)       # (as a click would)
        assert panel.list_changes.item(1).checkState() == Qt.CheckState.Checked and len(dlg._draft.kept) == 2
        dlg.cancel_ai()
    finally:
        released.append(1)
    assert panel.list_changes.isEnabled() and dlg.banner.btn_keep.isEnabled()
    dlg.keep_draft()
    assert [s["duration_sec"] for s in dlg.data["steps"]] == [10, 2, 30]


def test_a_draft_that_cant_be_shown_leaves_the_builder_unlocked(make, monkeypatch):
    from src.gui.control.dialogs.recipe_builder import dialog as D
    dlg = make()
    dlg.model.scripts.append([propose(changed(dlg, lambda d: d["steps"].pop()))])

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(D.ai_changes, "compute", boom)
    dlg.request_draft("drop the last step")
    assert not dlg._busy() and dlg.btn_save.isEnabled() and dlg.draft_bar.state == "idle"
    assert "couldn't be shown (RuntimeError: boom)" in dlg.draft_bar.info.text()
    assert len(dlg.data["steps"]) == 3


def test_a_change_left_out_shows_the_step_as_it_stays(make, folder):
    """A change to a PARALLEL step left out listed the draft's sub-steps, with the problems of the
    recipe's own on them."""
    from src.gui.control.dialogs.recipe_builder.outline import MARK, TEXT, WAS
    (folder / "par.json").write_text(json.dumps({"recipe_name": "Par", "version": "1.0", "steps": [
        {"step_id": 1, "type": "WAIT_TIME", "duration_sec": 1},
        {"step_id": 2, "type": "PARALLEL", "steps": [
            {"type": "ACTION", "target": "ion_beam.source.extraction.sp_requested_voltage", "value": 99},
            {"type": "WAIT_TIME", "duration_sec": 2}]}]}))
    dlg = make("par.json")
    dlg.model.scripts.append([propose(changed(dlg, lambda d: d["steps"][1]["steps"].pop(0)))])
    dlg.request_draft("drop the setpoint")
    change = dlg._draft.changes[0]
    assert change.kind == "change"
    top = dlg.outline.topLevelItem(1)
    assert top.childCount() == 1 and top.data(0, WAS).startswith("was: ")
    dlg.toggle_change(change.id, False)
    top = dlg.outline.topLevelItem(1)
    assert top.childCount() == 2 and "left out" in top.data(0, WAS)
    assert "99" in top.child(0).data(0, TEXT) and "error" in top.child(0).data(0, MARK)
    dlg.outline.select_path((1, 0), quiet=False)
    dlg._show_selected((1, 0))
    assert dlg.editor.step["value"] == 99


def test_a_note_on_a_step_changed_since_offers_no_fix(make):
    dlg = make()
    dlg.model.scripts.append([Reply(calls=[Call("submit_review", {"summary": "One.", "notes": [
        {"step": "2", "severity": "concern", "text": "2 s is short.", "fix": "Make step 2 five seconds"}]}, "r")])])
    dlg.request_review("")
    note = dlg._review[0].review.notes[0]
    item = dlg.draft_panel.list_notes.item(0)
    assert dlg.draft_panel.list_notes.itemWidget(item).btn_fix is not None
    dlg.outline.select_path((0,), quiet=False)
    dlg._show_selected((0,))
    dlg.add_block("wait_time")                          # step 2 is another step now
    dlg._check()
    item = dlg.draft_panel.list_notes.item(0)
    assert dlg.draft_panel.list_notes.itemWidget(item).btn_fix is None
    sessions = len(dlg.model.sessions)
    dlg.draft_fix(note)
    assert len(dlg.model.sessions) == sessions and dlg._job is None


def test_the_stop_conditions_tab_shows_the_drafts_list(make):
    dlg = make()
    dlg.model.scripts.append([propose(changed(dlg, lambda d: d.__setitem__("fault_policy", [TRIP])))])
    dlg.request_draft("stop if the relay trips")
    assert dlg.policy.list.count() == 1 and dlg.tabs.tabText(1) == "Stop conditions (1)"
    assert dlg.data["fault_policy"] == []
    dlg.toggle_change(dlg._draft.changes[0].id, False)
    assert dlg.policy.list.count() == 0 and dlg.tabs.tabText(1) == "Stop conditions (0)"
    dlg.discard_draft()
    assert dlg.policy.policy is dlg.data["fault_policy"]


def test_closing_while_a_draft_runs_cancels_it_only_if_the_window_closes(make, monkeypatch, app):
    from PyQt6.QtGui import QCloseEvent
    from src.gui.control.dialogs.recipe_builder import dialog as D
    dlg = make(threaded=True)
    dlg.le_name.setText("Waits, changed")              # (unsaved changes: closing asks)
    released = []

    def slow():
        while not released:
            time.sleep(0.01)
        return propose(changed(dlg, lambda d: d["steps"].pop()))
    dlg.model.scripts.append([slow])
    try:
        dlg.request_draft("drop the last step")
        monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_unsaved", lambda self: "cancel")
        event = QCloseEvent()
        dlg.closeEvent(event)
        assert not event.isAccepted() and dlg._job is not None and not dlg._job.cancelled()
        job = dlg._job
        monkeypatch.setattr(D.RecipeBuilderDialog, "_ask_unsaved", lambda self: "discard")
        assert dlg._may_discard() and dlg._job is None and job.cancelled()
    finally:
        released.append(1)



def test_the_connection_test_says_what_answered(make, monkeypatch):
    """Josh, 21 October 2026: a draft seemed to hang, with no way to tell why."""
    from src.core.recipe_ai import provider as P
    dlg = make()
    monkeypatch.setattr(P, "make_provider", dlg.model.provider)
    dlg.model.scripts.append([Reply(text="OK", model="gemini-x")])
    assert not dlg.draft_panel.btn_test.isHidden()
    dlg.draft_panel.btn_test.click()
    assert dlg._test_job is None and dlg.draft_panel.btn_test.isEnabled()
    assert dlg.draft_panel.lbl_test.text().startswith("Connected: gemini-x answered in")
    assert not dlg._busy()                                  # (it locks nothing)
    dlg.model.scripts.append([AIError("network", "No connection to the service within 15 s: is this PC on the "
                                                 "internet?")])
    dlg.draft_panel.btn_test.click()
    assert dlg.draft_panel.lbl_test.text() == ("Not connected: No connection to the service within 15 s: is this PC "
                                               "on the internet?")


def test_the_first_wait_says_that_the_model_thinks_first(make):
    dlg = make()
    said = []
    dlg.model.scripts.append([propose(changed(dlg, lambda d: d["steps"].pop()))])
    original = dlg._ai_progress
    dlg._ai_progress = lambda job, text: (said.append(text), original(job, text))
    dlg.request_draft("drop the last step")
    assert any("first answer (it thinks before it answers" in t for t in said), said
