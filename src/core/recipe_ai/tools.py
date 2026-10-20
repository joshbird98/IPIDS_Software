"""What the model may do in a session, and what it is told back:

    find_tags, describe_tag     the tag registry (the catalogue the checks use)
    read_help, read_recipe      the help's pages, a saved recipe or a template
    check_recipe                the checks the sequencer makes before it runs a recipe, and the recipe
                                in words, as the operator will read it
    propose_recipe              (drafting) the end: the whole recipe, checked - with errors it goes back
                                to the model, a few times, then is shown as it is
    submit_review               (reviewing) the end: notes on the recipe

Nothing here can reach the machine, write a file or read outside the help, the recipes and the
templates. A tool never raises: whatever the model sends, it gets an answer it can act on.
"""
import difflib
import inspect
import json
import os
import re
from dataclasses import dataclass, field

from src.core import paths
from src.core.recipe_ai.changes import compute, same
from src.core.recipe_ai.knowledge import HELP_PAGES, TEMPLATES_DIR, _kind_of, compact
from src.core.recipe_catalog import TURBO_ENABLE, Catalog
from src.core.recipe_check import ERROR, check_recipe
from src.core.recipe_store import read_recipe, renumber
from src.core.recipe_text import describe_policy_entry, describe_recipe, describe_step

SEVERITIES = ("concern", "suggestion", "question")
KNOWN_KEYS = ("recipe_name", "version", "description", "fault_policy", "steps")
SYNONYMS = {"bias": "smu", "cesium": "caesium", "vacuum": "pressure", "ioniser": "thermionic", "ionizer": "thermionic",
            "cathode": "target", "cup": "faraday", "hv": "voltage"}
HELP_LIMIT = 40000
# What a recipe needs to know about some commands, that the registry doesn't say
TAG_NOTES = {
    "ion_beam.beamline.magnet.cmd_degauss": "The magnet service steps the current through its degauss list (about "
                                            "2 minutes) and ends at 0 A with the output on. It needs the safety relay "
                                            "on, and the Faraday cup in if the source is running.",
    "ion_beam.beamline.steerer_magnet.cmd_degauss": "As the analyser magnet's degauss: about 2 minutes, ending at 0 A.",
    "ion_beam.beamline.diagnostics.cmd_insert_fc": "true inserts the cup, false retracts it; check "
                                                   "ion_beam.beamline.diagnostics.stat_fc_inserted (or _retracted), "
                                                   "within 15 s.",
    "ion_beam.source.chamber.cmd_open_gv": "Opens only with the PLC's permissive (the vacuum good, VG1 below "
                                           "5e-7 mbar in the procedure); safety_relay_init opens it for you.",
    "ion_beam.system.cmd_enable_safety": "1 only asks for the relay: the operator must turn the key and press "
                                         "START. Use safety_relay_init to bring it on; 0 withdraws the request (the "
                                         "PLC keeps the relay on while the caesium is hot).",
    "ion_beam.internal.cs_is_hot": "The PLC's flag: 1 while the caesium is hot; it clears at about 30 °C. While "
                                   "it is 1 the PLC won't let the target, filament or thermionic be switched off.",
    "ion_beam.system.cmd_source_shutdown": "1 hands the shutdown to the PLC (about 20 minutes): "
                                           "ion_beam.system.src_auto_sdown reads 1 while it runs. See the "
                                           "shutdown_by_plc template.",
}
TURBO_WHY = "left alone: trained staff run the source turbo pump by hand, never a recipe"

_RECIPE_JSON = {"type": "string", "description": "The whole recipe as JSON text: recipe_name, description, "
                                                 "fault_policy and steps"}


@dataclass
class Proposal:
    recipe: dict
    summary: str
    assumptions: list
    values_to_check: list
    problems: list                  # what the checks find in it (errors only if it couldn't be fixed)


@dataclass
class Note:
    where: str                      # "step 3", "step 5.2", "stop conditions", "" (the whole recipe)
    path: tuple                     # (2,), (4, 1), ("policy",), ()
    severity: str
    text: str
    fix: str = ""


@dataclass
class Review:
    summary: str
    notes: list = field(default_factory=list)


def parse_step_ref(ref, data: dict) -> tuple:
    """('step 5.2', (4, 1)) for '5.2' (or 'step 5.2') if the recipe has that step; ('stop conditions',
    ('policy',)); ('', ()) for anything else - the whole recipe."""
    text = str(ref or "").strip().lower()
    if not text:
        return "", ()
    if "stop" in text or "fault" in text or "policy" in text:
        return "stop conditions", ("policy",)
    found = re.fullmatch(r"(?:steps?\s*)?(\d+(?:\.\d+)*)", text)
    if not found:
        return "", ()
    path = tuple(int(x) - 1 for x in found.group(1).split("."))
    steps = data.get("steps") if isinstance(data, dict) else None
    try:
        for depth, i in enumerate(path):
            if i < 0 or not isinstance(steps, list):
                raise IndexError
            step = steps[i]
            steps = step.get("steps") if isinstance(step, dict) else None
    except IndexError:
        return "", ()
    return "step " + found.group(1), path


class Toolbox:
    def __init__(self, mode: str, catalog: Catalog, base: dict, recipes_dir: str = None, recipe_path: str = None,
                 names: dict = None, max_proposals: int = 3, templates_dir: str = TEMPLATES_DIR,
                 docs_dir: str = paths.DOCS_DIR):
        self.mode = mode                    # "draft" or "review"
        self.catalog = catalog
        self.base = base                    # the recipe as the operator has it
        self.recipes_dir = recipes_dir or paths.RECIPES_DIR
        self.recipe_path = recipe_path      # its file (sub-recipes are found beside it), None if new
        self.names = names or {}
        self.max_proposals = max_proposals
        self.templates_dir = templates_dir
        self.docs_dir = docs_dir
        self.proposal = None
        self.review = None
        self.rejected = 0                   # proposals sent back for errors
        self.calls = []                     # (name, args, answer) for the session log

    @property
    def finished(self) -> bool:
        return self.proposal is not None if self.mode == "draft" else self.review is not None

    # ------------------------------------------------------------------ the tools, as the model sees them
    def specs(self) -> list:
        tools = [
            {"name": "find_tags",
             "description": "Search the machine's tags by what they are called (e.g. 'einzel current', 'gate valve "
                            "open', 'VG1', 'caesium temperature') or by part of the tag name. Returns up to 15 tags: "
                            "what each is, its type, unit and range, and whether a recipe may set it.",
             "parameters": {"type": "object", "properties": {
                 "query": {"type": "string", "description": "Words to look for"},
                 "settable_only": {"type": "boolean", "description": "Only tags a recipe may set"}},
                 "required": ["query"]}},
            {"name": "describe_tag",
             "description": "Everything about one tag: what it is, its type, unit and range, whether a recipe may "
                            "set it (and why not), the registry's description, and the supply it belongs to.",
             "parameters": {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]}},
            {"name": "read_help",
             "description": "Read one of SINDIE's help pages (the procedures, the panels, the recipe help, the safety "
                            "rules), by its id from the list in your instructions.",
             "parameters": {"type": "object", "properties": {
                 "page": {"type": "string", "enum": list(HELP_PAGES)}}, "required": ["page"]}},
            {"name": "read_recipe",
             "description": "Read a saved recipe (its file in the recipes folder, e.g. 'auto_tune.json') or a template "
                            "('template:start_up.json'): the JSON, and the recipe in words.",
             "parameters": {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]}},
            {"name": "check_recipe",
             "description": "Check a recipe as the sequencer will before it runs it. Returns its errors (the sequencer "
                            "would refuse it), its warnings (it would run, but probably not as meant), and the recipe "
                            "in words as the operator will read it: read them to see it does what was asked.",
             "parameters": {"type": "object", "properties": {"recipe_json": _RECIPE_JSON},
                            "required": ["recipe_json"]}},
        ]
        if self.mode == "draft":
            tools.append(
                {"name": "propose_recipe",
                 "description": "Finish: propose the whole recipe to the operator, who sees it as changes to keep or "
                                "discard. It is checked first: with errors it comes back to you to fix.",
                 "parameters": {"type": "object", "properties": {
                     "recipe_json": _RECIPE_JSON,
                     "summary": {"type": "string",
                                 "description": "A few plain sentences for the operator: what you did, and anything "
                                                "you didn't do and why"},
                     "assumptions": {"type": "array", "items": {"type": "string"},
                                     "description": "What you assumed that the request didn't say"},
                     "values_to_check": {"type": "array", "items": {"type": "string"},
                                         "description": "Values you chose that the operator should check, e.g. "
                                                        "'Extraction 15 kV (typical: 15 to 20 kV)'"}},
                     "required": ["recipe_json", "summary"]}})
        else:
            tools.append(
                {"name": "submit_review",
                 "description": "Finish the review: your overall view and your notes (none if the recipe is good).",
                 "parameters": {"type": "object", "properties": {
                     "summary": {"type": "string", "description": "One or two sentences: your overall view"},
                     "notes": {"type": "array", "items": {"type": "object", "properties": {
                         "step": {"type": "string",
                                  "description": "The step it is about ('3', '5.2'), 'stop conditions', or '' for the "
                                                 "whole recipe"},
                         "severity": {"type": "string", "enum": list(SEVERITIES)},
                         "text": {"type": "string", "description": "The note: one or two plain sentences"},
                         "fix": {"type": "string",
                                 "description": "Optional: the change, as an instruction the drafter could carry out"}},
                         "required": ["step", "severity", "text"]}}},
                     "required": ["summary", "notes"]}})
        return tools

    # ------------------------------------------------------------------ running them
    def execute(self, name: str, args: dict) -> dict:
        args = args if isinstance(args, dict) else {}
        handler = getattr(self, f"_tool_{name}", None)
        allowed = {t["name"] for t in self.specs()}
        if handler is None or name not in allowed:
            answer = {"error": f"There is no tool {name!r}: use one of {', '.join(sorted(allowed))}."}
        else:
            kwargs = {k: v for k, v in args.items() if isinstance(k, str)}
            try:
                inspect.signature(handler).bind(**kwargs)
            except TypeError as e:
                answer = {"error": f"Wrong arguments for {name}: {e}"}
            else:
                try:
                    answer = handler(**kwargs)
                except Exception as e:      # a tool must answer, whatever the model sends
                    answer = {"error": f"{name} failed: {type(e).__name__}: {e}"}
        self.calls.append({"tool": name, "args": _short(args), "answer": _short(answer)})
        return answer

    def _tool_find_tags(self, query: str = "", settable_only: bool = False) -> dict:
        words = [w for w in re.split(r"[^\w°µ]+", str(query).lower()) if w]
        if not words:
            return {"error": "Say what to look for, e.g. 'extraction voltage'."}
        scored = []
        for t in self.catalog.readable():
            if settable_only and not _may_set(t):
                continue
            label, name, desc = t.label.lower(), t.name.lower(), t.description.lower()
            score = matched = 0
            for w in words:
                for word in (w, SYNONYMS.get(w)):
                    if not word:
                        continue
                    if re.search(rf"(?<![\w]){re.escape(word)}", label):
                        score, matched = score + 3, matched + 1
                        break
                    if word in name:
                        score, matched = score + 2, matched + 1
                        break
                    if word in desc:
                        score, matched = score + 1, matched + 1
                        break
            if matched == len(words):
                scored.append((-score, len(t.name), t))
        scored.sort(key=lambda x: x[:2])
        found = [self._brief(t) for _, _, t in scored[:15]]
        if not found:
            return {"found": [], "note": "Nothing matches every word: try fewer or other words (the readings list "
                                         "in your instructions has every tag)."}
        return {"found": found}

    def _brief(self, t) -> dict:
        out = {"tag": t.name, "what": t.label, "type": _kind_of(t),
               "recipe_may_set": _may_set(t)}
        if t.range_text():
            out["range"] = t.range_text()
        return out

    def _tool_describe_tag(self, tag: str = "") -> dict:
        t = self.catalog.tag(str(tag).strip())
        if t is None:
            close = difflib.get_close_matches(str(tag), [x.name for x in self.catalog.readable()], n=5, cutoff=0.6)
            return {"error": f"There is no tag {tag!r}.", "did_you_mean": close}
        out = self._brief(t)
        out["unit"] = t.unit
        if not _may_set(t):
            out["why_a_recipe_may_not_set_it"] = TURBO_WHY if t.name == TURBO_ENABLE else t.not_settable
        if t.description:
            out["registry_description"] = t.description
        if t.name in TAG_NOTES:
            out["note"] = TAG_NOTES[t.name]
        supply = self.catalog.supply(t.name)
        if supply is not None:
            out["supply"] = {"name": supply.name, "cmd_enable": supply.enable_tag, "stat_enabled": supply.enabled_tag,
                             "sp_tag": supply.setpoint_tag, "rb_tag": supply.readback_tag,
                             "usual_tolerance": supply.tolerance, "usual_timeout_sec": supply.timeout_s,
                             "wait_for_readback": supply.wait_by_default}
        return out

    def _tool_read_help(self, page: str = "") -> dict:
        entry = HELP_PAGES.get(str(page).strip())
        if entry is None:
            return {"error": f"No help page {page!r}: one of {', '.join(HELP_PAGES)}."}
        path, title = entry
        try:
            with open(os.path.join(self.docs_dir, path), "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            return {"error": f"The page can't be read: {e}"}
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("!["))
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text) > HELP_LIMIT:
            text = text[:text.rfind("\n", 0, HELP_LIMIT)] + "\n(The rest of the page is left out: it is long.)"
        return {"page": page, "title": title, "text": text}

    def _tool_read_recipe(self, file: str = "") -> dict:
        name = str(file).strip()
        folder = self.recipes_dir
        if name.startswith("template:"):
            name, folder = name[len("template:"):].strip(), self.templates_dir
        if not name or os.path.basename(name) != name or not name.endswith(".json"):
            return {"error": "Give the file's name only, e.g. 'auto_tune.json' or 'template:start_up.json'."}
        try:
            data, _ = read_recipe(os.path.join(folder, name))
        except (OSError, ValueError) as e:
            return {"error": f"{name} can't be read: {e}"}
        data.pop("template", None)
        return {"file": file, "recipe_json": compact(data), "in_words": describe_recipe(data, self.catalog, self.names)}

    def _parse(self, recipe_json=None, recipe=None):
        """(data, None) or (None, an answer saying what is wrong)."""
        if isinstance(recipe, dict):
            data = recipe
        else:
            try:
                data = json.loads(recipe_json) if isinstance(recipe_json, str) else recipe_json
            except ValueError as e:
                return None, {"error": f"recipe_json isn't valid JSON: {e}. Send the whole recipe as JSON text."}
        if not isinstance(data, dict):
            return None, {"error": "The recipe must be a JSON object with recipe_name, fault_policy and steps."}
        return self.prepare(data), None

    def prepare(self, data: dict) -> dict:
        """The recipe as the builder would hold it: its version left as the operator's (a version is
        given when it is saved), the steps numbered, anything that isn't part of a recipe left out."""
        out = {k: v for k, v in self.base.items() if k not in KNOWN_KEYS and k != "template"}
        out["recipe_name"] = str(data.get("recipe_name") or self.base.get("recipe_name") or "New recipe")
        out["version"] = str(self.base.get("version") or "1.0")
        description = data.get("description", self.base.get("description"))
        if str(description or "").strip():
            out["description"] = str(description)
        policy = data.get("fault_policy")
        out["fault_policy"] = [] if policy in (None, {}) else policy
        steps = data.get("steps")
        out["steps"] = [] if steps is None else steps
        if isinstance(out["steps"], list):
            renumber(out["steps"])
        return json.loads(json.dumps(out, default=str))

    def _check(self, data: dict) -> tuple:
        problems = check_recipe(data, self.catalog, self.recipes_dir, self.recipe_path)
        errors = [p.text() for p in problems if p.level == ERROR]
        warnings = [p.text() for p in problems if p.level != ERROR]
        return problems, errors, warnings

    def _tool_check_recipe(self, recipe_json=None, recipe=None) -> dict:
        data, wrong = self._parse(recipe_json, recipe)
        if wrong:
            return wrong
        _, errors, warnings = self._check(data)
        verdict = (f"{len(errors)} error(s): the sequencer would refuse it" if errors else
                   f"no errors; {len(warnings)} warning(s)" if warnings else "no errors or warnings")
        return {"verdict": verdict, "errors": errors, "warnings": warnings,
                "in_words": describe_recipe(data, self.catalog, self.names)}

    def _tool_propose_recipe(self, recipe_json=None, summary: str = "", assumptions=None, values_to_check=None,
                             recipe=None) -> dict:
        data, wrong = self._parse(recipe_json, recipe)
        if wrong:
            return wrong
        problems, errors, warnings = self._check(data)
        if errors and self.rejected + 1 < self.max_proposals:
            self.rejected += 1
            left = self.max_proposals - self.rejected
            return {"accepted": False, "errors": errors, "warnings": warnings,
                    "what_to_do": f"Not shown to the operator: the sequencer would refuse it. Fix every error and "
                                  f"call propose_recipe again with the whole recipe ({left} more "
                                  f"{'try' if left == 1 else 'tries'})."}
        changes = compute(self.base, data, lambda s: describe_step(s, self.catalog, self.names),
                          lambda e: describe_policy_entry(e, self.catalog))       # (before it is accepted)
        self.proposal = Proposal(data, str(summary or "").strip(), _strings(assumptions), _strings(values_to_check),
                                 problems)
        answer = {"accepted": True, "warnings": warnings,
                  "changes (numbered as in the operator's recipe now)": [c.title for c in changes]
                  or ["none: the recipe is unchanged"]}
        if errors:
            answer["note"] = "Shown to the operator with its errors (out of tries)."
        if same(self.base, data):
            answer["note"] = "It makes no changes; the operator is shown your summary."
        return answer

    def _tool_submit_review(self, summary: str = "", notes=None) -> dict:
        found = []
        for n in notes if isinstance(notes, list) else []:
            if not isinstance(n, dict) or not str(n.get("text") or "").strip():
                continue
            where, path = parse_step_ref(n.get("step"), self.base)
            severity = str(n.get("severity") or "").strip().lower()
            found.append(Note(where, path, severity if severity in SEVERITIES else "suggestion",
                              str(n["text"]).strip(), str(n.get("fix") or "").strip()))
        order = {s: i for i, s in enumerate(SEVERITIES)}
        found.sort(key=lambda n: order[n.severity])
        self.review = Review(str(summary or "").strip(), found)
        return {"accepted": True, "notes": len(found)}


def _may_set(tag) -> bool:
    return tag.settable and tag.name != TURBO_ENABLE


def _strings(value) -> list:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value or [] if str(v).strip()] if isinstance(value, (list, tuple)) else []


def _short(value, limit: int = 2000):
    """For the session log: long strings cut down."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"... ({len(value)} characters)"
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_short(v, limit) for v in value[:50]]
    return value
