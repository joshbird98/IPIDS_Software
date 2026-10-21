"""Recipe drafting's core (src/core/recipe_ai): the settings and the key, what the model is told,
its tools, the changes a proposal makes (and keeping some of them), a session's loop with a
scripted model, and the Gemini adapter through the real google-genai SDK against a fake service."""
import json
import os

import pytest

from src.core.recipe_ai import changes, knowledge, settings
from src.core.recipe_ai.agent import Session
from src.core.recipe_ai.provider import AIError, Call, Reply
from src.core.recipe_ai.tools import Toolbox, parse_step_ref
from src.core.recipe_catalog import TURBO_ENABLE, Catalog
from src.core.recipe_check import ERROR

EXT_SP = "ion_beam.source.extraction.sp_requested_voltage"
TRIP = {"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_0_system", "bit_str": "0.1",
        "name": "Safety_Relay_Tripped", "action": "ABORT"}


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load()


def recipe(*steps, name="Test", policy=None):
    return {"recipe_name": name, "version": "1.0", "fault_policy": policy or [], "steps": list(steps)}


def wait(s, comment=""):
    step = {"type": "WAIT_TIME", "duration_sec": s}
    if comment:
        step["comment"] = comment
    return step


# ------------------------------------------------------------------------------------------------ settings
def test_settings_defaults_and_bad_values_fall_back(tmp_path):
    path = tmp_path / "recipe_ai.json"
    path.write_text(json.dumps({"model": "gemini-4-argon", "max_rounds": 0, "timeout_s": "slow",
                                "fallback_models": ["a", 3], "thinking_level": "high", "temperature": None,
                                "enabled": True}))
    s = settings.Settings.load(str(path))
    assert s.model == "gemini-4-argon" and s.thinking_level == "high"
    assert s.max_rounds == settings.Settings().max_rounds          # 0: out of range
    assert s.timeout_s == settings.Settings().timeout_s            # not a number
    assert s.fallback_models == settings.Settings().fallback_models
    assert settings.Settings.load(str(tmp_path / "missing.json")) == settings.Settings()
    path.write_text(json.dumps({"fallback_models": ["b", "a", "b"]}))
    assert settings.Settings.load(str(path)).fallback_models == ("b", "a")
    (tmp_path / "bad.json").write_text("{not json")
    assert settings.Settings.load(str(tmp_path / "bad.json")) == settings.Settings()


def test_the_key_comes_from_the_environment_or_the_env_file(tmp_path, monkeypatch):
    for name in settings.KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings.paths, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert settings.api_key() == ("", "")
    (tmp_path / ".env").write_text("# keys\nOTHER=1\nexport GEMINI_API_KEY = \"abc 123\"  \n")
    assert settings.api_key() == ("abc 123", str(tmp_path / ".env"))
    (tmp_path / ".env").write_text("GEMINI_API_KEY=xyz # a comment\n")
    assert settings.api_key()[0] == "xyz"
    (tmp_path / ".env").write_text("GEMINI_API_KEY='abc'  # the lab's key\n")
    assert settings.api_key()[0] == "abc"
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    key, where = settings.api_key()
    assert key == "from-env" and "from-env" not in where and "GEMINI_API_KEY" in where


def test_availability_says_why_not(tmp_path, monkeypatch):
    for name in settings.KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings.paths, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    ok, why = settings.availability(settings.Settings())
    assert not ok and "GEMINI_API_KEY" in why and ".env" in why
    ok, why = settings.availability(settings.Settings(enabled=False))
    assert not ok and "switched off" in why
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    real = settings.importlib.util.find_spec

    def no_genai(name, *a):
        return None if name == "google.genai" else real(name, *a)
    monkeypatch.setattr(settings.importlib.util, "find_spec", no_genai)
    ok, why = settings.availability(settings.Settings())
    assert not ok and "pip install" in why


# ------------------------------------------------------------------------------------------------ the prompt
def test_the_prompt_is_made_from_the_catalogue_the_checks_use(catalog):
    text = knowledge.system_prompt(catalog, "draft")
    for t in catalog.settable():
        if t.name != TURBO_ENABLE:
            assert t.name in text, t.name
    assert f"- {TURBO_ENABLE}:" not in text                       # left alone, and not offered
    for s in catalog.supplies:
        assert s.setpoint_tag in text and s.readback_tag in text
    from src.core.routines import ROUTINE_REGISTRY
    for name, cls in ROUTINE_REGISTRY.items():
        assert f"### {name}" in text
        for p in cls.PARAMS:
            if p.key != "gv_stat_closed":
                assert f"- {p.key} (" in text, (name, p.key)
    assert "Safety_Relay_Tripped" in text and "### start_up.json" in text and "beam-start-up" in text
    for placeholder in ("{relay}", "{turbo}", "{role}", "{use}", "{fault_reset}"):
        assert placeholder not in text
    assert len(text) < 90000                                      # (the old assistant sent the whole registry)
    review = knowledge.system_prompt(catalog, "review")
    assert "## How to review" in review and "## How to work" not in review and "submit_review" in review


def test_the_request_shows_the_recipe_its_problems_and_the_selected_step(catalog):
    data = recipe(wait(5, "Settle"), {"type": "ACTION", "target": EXT_SP, "value": 99})
    from src.core.recipe_check import check_recipe
    text = knowledge.draft_task("make it 10 s", data, check_recipe(data, catalog), (0,), None, catalog)
    assert '"make it 10 s"' in text and "Wait 5 s" in text and "step 1 - \"Wait 5 s\"" in text
    assert "- error: step 2: 99 kV is outside" in text
    text = knowledge.draft_task("x", data, [], None, "a.json", catalog, earlier=recipe(wait(9)), unticked=["Add 1 step"])
    assert "its file is a.json" in text and "earlier draft" in text and "- Add 1 step" in text
    assert "No step is selected" in text


# ------------------------------------------------------------------------------------------------ tools
def test_find_and_describe_tags(catalog):
    box = Toolbox("draft", catalog, recipe())
    found = box.execute("find_tags", {"query": "einzel current"})["found"]
    assert found[0]["tag"] == "ion_beam.source.einzel.rb_current"
    assert all(f["recipe_may_set"] for f in box.execute("find_tags", {"query": "cup bias",
                                                                       "settable_only": True})["found"])
    assert box.execute("find_tags", {"query": ""})["error"]
    wrong = box.execute("describe_tag", {"tag": "ion_beam.source.einzel.rb_curent"})
    assert "ion_beam.source.einzel.rb_current" in wrong["did_you_mean"]
    turbo = box.execute("describe_tag", {"tag": TURBO_ENABLE})
    assert turbo["recipe_may_set"] is False and "by hand" in turbo["why_a_recipe_may_not_set_it"]
    assert box.execute("describe_tag", {"tag": EXT_SP})["supply"]["rb_tag"] == "ion_beam.source.extraction.rb_voltage"


def test_help_and_recipes_are_read_only_from_their_folders(catalog):
    box = Toolbox("draft", catalog, recipe())
    page = box.execute("read_help", {"page": "beam-start-up"})
    assert "Bringing up a beam" in page["text"] and "![" not in page["text"]
    assert "error" in box.execute("read_help", {"page": "../../README"})
    assert "error" in box.execute("read_recipe", {"file": "../config/fault_config.json"})
    assert "error" in box.execute("read_recipe", {"file": "/etc/passwd"})
    template = box.execute("read_recipe", {"file": "template:degauss.json"})
    assert "Degauss" in template["in_words"]
    assert "error" in box.execute("no_such_tool", {})
    assert "error" in box.execute("submit_review", {"summary": "x", "notes": []})      # (a draft session's)


def test_a_proposal_with_errors_goes_back_then_is_shown_as_it_is(catalog):
    base = recipe(wait(5))
    box = Toolbox("draft", catalog, base, max_proposals=2)
    bad = recipe(wait(5), {"type": "ACTION", "target": EXT_SP, "value": 99})
    answer = box.execute("propose_recipe", {"recipe_json": json.dumps(bad), "summary": "x"})
    assert answer["accepted"] is False and answer["errors"] and box.proposal is None
    assert "1 more try" in answer["what_to_do"]
    answer = box.execute("propose_recipe", {"recipe_json": json.dumps(bad), "summary": "x"})
    assert answer["accepted"] is True and "out of tries" in answer["note"]
    assert any(p.level == ERROR for p in box.proposal.problems)
    assert box.execute("propose_recipe", {"recipe_json": "{not json"})["error"].startswith("recipe_json isn't")


def test_a_proposal_out_of_tries_with_stop_conditions_that_arent_objects_is_shown(catalog):
    """Found in review: its changes were worked out after it was accepted, and raised on them."""
    box = Toolbox("draft", catalog, recipe(wait(5)), max_proposals=1)
    odd = dict(recipe(wait(5)), fault_policy=["Safety_Relay_Tripped"])
    answer = box.execute("propose_recipe", {"recipe_json": json.dumps(odd), "summary": "x"})
    assert answer["accepted"] is True and box.proposal is not None
    assert answer["changes (numbered as in the operator's recipe now)"] == ["Stop conditions: add"]


def test_wrong_arguments_and_a_fault_in_a_tool_are_told_apart(catalog, monkeypatch):
    box = Toolbox("draft", catalog, recipe())
    assert box.execute("find_tags", {"querry": "x"})["error"].startswith("Wrong arguments for find_tags")
    from src.core.recipe_ai import tools

    def broken(*a, **k):
        raise TypeError("inside the checks")
    monkeypatch.setattr(tools, "check_recipe", broken)
    answer = box.execute("check_recipe", {"recipe_json": json.dumps(recipe(wait(1)))})
    assert answer["error"] == "check_recipe failed: TypeError: inside the checks"


def test_a_proposal_keeps_the_operators_version_and_is_numbered(catalog):
    base = dict(recipe(wait(5)), version="2.3")
    box = Toolbox("draft", catalog, base)
    proposed = {"recipe_name": "Test", "version": "9.9", "steps": [wait(1), wait(2)], "template": True}
    answer = box.execute("propose_recipe", {"recipe_json": json.dumps(proposed), "summary": "Two waits",
                                            "values_to_check": "1 s"})
    assert answer["accepted"] is True
    p = box.proposal
    assert p.recipe["version"] == "2.3" and [s["step_id"] for s in p.recipe["steps"]] == [1, 2]
    assert "template" not in p.recipe and p.values_to_check == ["1 s"] and p.recipe["fault_policy"] == []


def test_review_notes_point_at_steps(catalog):
    data = recipe(wait(1), {"type": "PARALLEL", "steps": [wait(2), wait(3)]})
    assert parse_step_ref("2.2", data) == ("step 2.2", (1, 1))
    assert parse_step_ref("step 1", data) == ("step 1", (0,))
    assert parse_step_ref("stop conditions", data) == ("stop conditions", ("policy",))
    assert parse_step_ref("7", data) == ("", ())
    assert parse_step_ref("", data) == ("", ())
    box = Toolbox("review", catalog, data)
    box.execute("submit_review", {"summary": "ok", "notes": [
        {"step": "1", "severity": "suggestion", "text": "Longer?"},
        {"step": "2.1", "severity": "concern", "text": "Too short.", "fix": "Make it 10 s"},
        {"step": "1", "severity": "odd", "text": "x"}, {"step": "1", "severity": "concern", "text": " "}]})
    assert [n.severity for n in box.review.notes] == ["concern", "suggestion", "suggestion"]
    assert box.review.notes[0].path == (1, 0) and box.review.notes[0].fix == "Make it 10 s"


# ------------------------------------------------------------------------------------------------ changes
def describe(step):
    return f"{step.get('type')} {step.get('duration_sec', step.get('value', ''))}"


def test_changes_a_step_changed_beside_one_added_reads_as_both():
    base = recipe(wait(1), wait(2), wait(3))
    proposed = recipe(wait(1), wait(20), wait(25), wait(3))
    found = changes.compute(base, proposed, describe)
    assert [(c.kind, c.title) for c in found] == [("change", "Change step 2"), ("add", "Add 1 step after step 2")]
    assert found[0].was == ("WAIT_TIME 2",) and found[0].lines == ("WAIT_TIME 20",)


def test_changes_ignore_numbers_spelling_and_field_order():
    base = recipe({"step_id": 1, "type": "ACTION", "target": EXT_SP, "value": True, "comment": "On"})
    proposed = recipe({"comment": "On", "value": 1, "target": EXT_SP, "type": "ACTION"})
    assert changes.compute(base, proposed) == [] and changes.same(base, proposed)
    kept = changes.apply(base, proposed, [], set())
    assert kept["steps"][0]["value"] is True                     # (the operator's own spelling)


def test_keeping_some_changes_and_not_others():
    base = recipe(wait(1, "a"), wait(2, "b"), wait(3, "c"), name="Old")
    proposed = recipe(wait(1, "a"), wait(3, "c"), wait(4, "d"), name="New", policy=[TRIP])
    found = changes.compute(base, proposed, describe)
    kinds = {c.kind: c for c in found}
    assert set(kinds) == {"name", "remove", "add", "stops"}
    only_add = changes.apply(base, proposed, found, {kinds["add"].id})
    assert [s["duration_sec"] for s in only_add["steps"]] == [1, 2, 3, 4]
    assert only_add["recipe_name"] == "Old" and only_add["fault_policy"] == []
    assert [s["step_id"] for s in only_add["steps"]] == [1, 2, 3, 4]
    everything = changes.apply(base, proposed, found, {c.id for c in found})
    assert [s["duration_sec"] for s in everything["steps"]] == [1, 3, 4]
    assert everything["recipe_name"] == "New" and everything["fault_policy"] == [TRIP]
    assert base["steps"][1]["duration_sec"] == 2 and "step_id" not in base["steps"][0]    # the base untouched
    rows = changes.merged(base, proposed, found)
    assert [(r.source, r.index, r.kind) for r in rows] == [("base", 0, ""), ("base", 1, "remove"), ("base", 2, ""),
                                                            ("new", 2, "add")]
    assert changes.numbers(rows, set()) == [1, 2, 3, None]
    assert changes.numbers(rows, {c.id for c in found}) == [1, None, 2, 3]


def test_a_new_recipe_is_added_whole():
    found = changes.compute(recipe(), recipe(wait(1), wait(2)), describe)
    assert [c.title for c in found] == ["Add 2 steps"]


# ------------------------------------------------------------------------------------------------ a session
class _Chat:
    model = "fake-1"

    def __init__(self, script):
        self.script, self.sent = list(script), []

    def send(self, text=None, results=None):
        self.sent.append((text, results))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Provider:
    def __init__(self, script):
        self.chat = _Chat(script)

    def start(self, system, tools):
        self.system, self.tools = system, tools
        return self.chat


def _propose(data, summary="Done.", **extra):
    return Reply(calls=[Call("propose_recipe", dict({"recipe_json": json.dumps(data), "summary": summary}, **extra),
                             "p")], usage={"input": 1000, "output": 50})


def _session(catalog, script, base=None, mode="draft", **kw):
    provider = _Provider(script)
    progress = []
    s = Session(mode, catalog, base or recipe(wait(5)), provider=provider, progress=progress.append, **kw)
    return s, provider, progress


def test_a_draft_that_is_fixed_after_being_sent_back(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.recipe_ai.agent.LOG_DIR", str(tmp_path))
    bad = recipe(wait(5), {"type": "ACTION", "target": EXT_SP, "value": 99})
    good = recipe(wait(5), wait(120, "Two minutes"))
    s, provider, progress = _session(catalog, [
        Reply(calls=[Call("find_tags", {"query": "extraction voltage"}, "a")]),
        _propose(bad), Reply(text="Hmm."), _propose(good, "Added a 2 min wait.", values_to_check=["2 min"])])
    outcome = s.draft("add a two minute wait", selected=(0,))
    assert outcome.proposal.summary == "Added a 2 min wait." and outcome.rounds == 4
    assert outcome.usage["input"] == 2000
    texts = [t for t, _r in provider.chat.sent]
    assert "add a two minute wait" in texts[0] and "To finish, call propose_recipe" in texts[3]
    assert any("sent back to be fixed" in p for p in progress) and any("Looking up tags" in p for p in progress)
    logged = json.loads(open(next(tmp_path.glob("*.jsonl"))).read().splitlines()[-1])
    assert logged["request"] == "add a two minute wait" and logged["proposal"]["summary"] == "Added a 2 min wait."
    assert len(logged["calls"]) == 3


def test_a_refinement_answers_the_last_calls_first(catalog):
    first, second = recipe(wait(5), wait(10)), recipe(wait(5), wait(20))
    s, provider, _ = _session(catalog, [_propose(first), _propose(second, "20 s now.")])
    s.draft("add a wait")
    outcome = s.refine("make it 20 s", unticked=["Change step 1"])
    text, results = provider.chat.sent[-1]
    assert results and results[0][0].name == "propose_recipe" and results[0][1]["accepted"] is True
    assert "make it 20 s" in text and "- Change step 1" in text
    assert outcome.proposal.recipe["steps"][1]["duration_sec"] == 20


def test_a_session_gives_up_and_says_why(catalog):
    s, _, _ = _session(catalog, [Reply(text="I think...")] * 3)
    with pytest.raises(AIError) as e:
        s.draft("x")
    assert e.value.kind == "gave_up" and "I think" in e.value.message
    looping = [Reply(calls=[Call("find_tags", {"query": "x"}, str(i))]) for i in range(30)]
    s, _, _ = _session(catalog, looping, settings=settings.Settings(max_rounds=3))
    with pytest.raises(AIError) as e:
        s.draft("x")
    assert e.value.kind == "gave_up" and "3 answers" in e.value.message


def test_a_malformed_call_is_asked_again_and_a_cancel_stops(catalog):
    s, provider, _ = _session(catalog, [Reply(finish="MALFORMED_FUNCTION_CALL"), _propose(recipe(wait(6)))])
    s.draft("x")
    assert "couldn't be read" in provider.chat.sent[1][0]
    s, _, _ = _session(catalog, [Reply(calls=[Call("find_tags", {"query": "x"}, "a")]), _propose(recipe(wait(6)))])
    s.cancelled = lambda: True
    with pytest.raises(AIError) as e:
        s.draft("x")
    assert e.value.kind == "cancelled"


def test_a_failure_from_the_service_reaches_the_operator_as_is(catalog):
    s, _, _ = _session(catalog, [AIError("quota", "The service's rate limit is used up.")])
    with pytest.raises(AIError) as e:
        s.draft("x")
    assert e.value.kind == "quota"


def test_a_review_session(catalog):
    data = recipe(wait(5), wait(6))
    s, _, _ = _session(catalog, [Reply(calls=[Call("submit_review", {"summary": "Fine.", "notes": [
        {"step": "2", "severity": "question", "text": "Why 6 s?"}]}, "r")])], base=data, mode="review")
    outcome = s.review("the waits")
    assert outcome.review.summary == "Fine." and outcome.review.notes[0].path == (1,)


# ------------------------------------------------------------------------------------------------ Gemini
def _gemini(handler, monkeypatch, **overrides):
    pytest.importorskip("google.genai")
    httpx = pytest.importorskip("httpx")
    from src.core.recipe_ai import provider as P
    monkeypatch.setattr(P, "wait", lambda seconds, cancelled=None: None)
    s = settings.Settings(**overrides)
    progress = []
    return P.GeminiProvider(s, "test-key-0123", progress=progress.append,
                            http_client=httpx.Client(transport=httpx.MockTransport(handler))), progress


TOOLS = [{"name": "find_tags", "description": "d", "parameters": {
    "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]


def _answer(parts, **extra):
    body = {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10}, "modelVersion": "gemini-x"}
    body.update(extra)
    return body


def test_gemini_sends_the_conversation_back_with_its_signatures(monkeypatch):
    import httpx
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content), request.headers))
        if settings.Settings().model in str(request.url):
            return httpx.Response(404, json={"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})
        if len(seen) == 3:
            return httpx.Response(503, json={"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
        if len(seen) == 2:
            return httpx.Response(200, json=_answer([{"functionCall": {"name": "find_tags", "args": {"query": "q"},
                                                                       "id": "c1"}, "thoughtSignature": "c2ln"}]))
        return httpx.Response(200, json=_answer([{"text": "Done."}]))

    provider, progress = _gemini(handler, monkeypatch, fallback_models=("gemini-b",))
    chat = provider.start("SYSTEM", TOOLS)
    reply = chat.send(text="hello")
    assert reply.calls == [Call("find_tags", {"query": "q"}, "c1")] and reply.usage["input"] == 100
    assert provider.model == "gemini-b" and any("gemini-b" in p for p in progress)
    reply = chat.send(results=[(reply.calls[0], {"found": []})])
    assert reply.text == "Done." and any("busy" in p for p in progress)
    url, body, headers = seen[-1]
    assert "test-key-0123" not in url and headers.get("x-goog-api-key") == "test-key-0123"
    assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM"
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "find_tags"
    model_turn, answer_turn = body["contents"][1], body["contents"][2]
    assert model_turn["parts"][0]["thoughtSignature"] == "c2ln"
    assert answer_turn["parts"][0]["functionResponse"] == {"id": "c1", "name": "find_tags", "response": {"found": []}}


@pytest.mark.parametrize("status,body,kind", [
    (400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                     "status": "INVALID_ARGUMENT"}}, "auth"),
    (403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}, "auth"),
    (429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                     "details": [{"retryDelay": "45s"}]}}, "quota"),
    (404, {"error": {"code": 404, "message": "no model", "status": "NOT_FOUND"}}, "model"),
])
def test_gemini_failures_are_said_for_the_operator(monkeypatch, status, body, kind):
    import httpx
    provider, _ = _gemini(lambda request: httpx.Response(status, json=body), monkeypatch, fallback_models=())
    with pytest.raises(AIError) as e:
        provider.start("S", TOOLS).send(text="x")
    assert e.value.kind == kind and "test-key-0123" not in e.value.message


def test_gemini_retries_a_busy_service_twice_then_says_so(monkeypatch):
    """Found in review: a short retryDelay was retried for ever."""
    import httpx
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(429, json={"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                                                   "details": [{"retryDelay": "17s"}]}})
    provider, progress = _gemini(handler, monkeypatch, fallback_models=())
    with pytest.raises(AIError) as e:
        provider.start("S", TOOLS).send(text="x")
    assert e.value.kind == "quota" and len(seen) == 3
    assert [p for p in progress if "busy" in p] == ["The service is busy: trying again in 17 s"] * 2


def test_gemini_tries_each_model_once(monkeypatch):
    """Found in review: a model named twice in the fallbacks was asked for again and again."""
    import httpx
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(404, json={"error": {"code": 404, "message": "no model", "status": "NOT_FOUND"}})
    provider, _ = _gemini(handler, monkeypatch, fallback_models=("gemini-b", settings.Settings().model, "gemini-b"))
    with pytest.raises(AIError) as e:
        provider.start("S", TOOLS).send(text="x")
    assert e.value.kind == "model" and len(seen) == 2 and "gemini-b" in seen[1]


def test_gemini_network_failure(monkeypatch):
    import httpx

    def handler(request):
        raise httpx.ConnectError("no route", request=request)
    provider, _ = _gemini(handler, monkeypatch)
    with pytest.raises(AIError) as e:
        provider.start("S", TOOLS).send(text="x")
    assert e.value.kind == "network" and "online" in e.value.message


def test_gemini_withheld_answer(monkeypatch):
    import httpx
    body = {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "SAFETY"}]}
    provider, _ = _gemini(lambda request: httpx.Response(200, json=body), monkeypatch)
    with pytest.raises(AIError) as e:
        provider.start("S", TOOLS).send(text="x")
    assert e.value.kind == "blocked"


def test_gemini_requests_get_a_short_connect_limit_and_a_long_answer_limit(monkeypatch):
    """google-genai gives every phase of a request one limit (and replaces a client's own), so a PC
    that couldn't reach the service waited the whole 120 s to say so."""
    import httpx
    seen = []

    def handler(request):
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json=_answer([{"text": "OK"}]))
    provider, _ = _gemini(handler, monkeypatch, timeout_s=90.0)
    provider.start("S", TOOLS).send(text="x")
    assert seen == [{"connect": 15.0, "pool": 15.0, "read": 90.0, "write": 90.0}]


@pytest.mark.parametrize("raised,kind,said", [
    ("ConnectTimeout", "network", "No connection to the service within 15 s"),
    ("ReadTimeout", "timeout", "didn't answer within 120 s"),
    ("ProxyError", "network", "The proxy refused"),
    ("ConnectError", "network", "Can't reach the service"),
])
def test_gemini_network_failures_say_which(monkeypatch, raised, kind, said):
    import httpx

    def handler(request):
        raise getattr(httpx, raised)("no", request=request)
    provider, _ = _gemini(handler, monkeypatch)
    with pytest.raises(AIError) as e:
        provider.start("S", TOOLS).send(text="x")
    assert e.value.kind == kind and said in e.value.message


def test_a_certificate_the_network_replaced_is_said_so():
    from src.core.recipe_ai.provider import classify
    err = classify(OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "m")
    assert err.kind == "network" and "SSL_CERT_FILE" in err.message


def test_the_connection_test_says_what_answered(monkeypatch, tmp_path):
    pytest.importorskip("google.genai")
    httpx = pytest.importorskip("httpx")
    from src.core.recipe_ai import provider as P
    for name in settings.KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings.paths, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=k-123\n")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=_answer([{"text": "OK"}]))))
    said = P.check_connection(settings.Settings(), http_client=client)
    assert said.startswith("Connected: gemini-x answered in") and str(tmp_path / ".env") in said
    assert "k-123" not in said


def test_no_key_no_provider(monkeypatch, tmp_path):
    from src.core.recipe_ai.provider import make_provider
    for name in settings.KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings.paths, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AIError) as e:
        make_provider(settings.Settings())
    assert e.value.kind == "no_key" and os.path.basename(".env") in e.value.message
