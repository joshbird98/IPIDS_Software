"""A drafting or review session: the operator's request, the model and its tools, until the model
proposes (or gives up). Blocking - the recipe builder runs it in a thread.

    session = Session("draft", catalog, recipe, filename, recipes_dir, progress=print)
    outcome = session.draft("after step 3, wait for the source pressure below 5e-6", selected=(2,))
    outcome = session.refine("make the wait 10 minutes", unticked=["Change step 6"])   # same conversation
    outcome = Session("review", catalog, recipe, ...).review()

outcome.proposal (tools.Proposal) or outcome.review (tools.Review), with the model, the number of
answers, the time and the tokens it took. Every failure is an AIError for the operator.

Each run is logged, one JSON line, in SystemLogs/recipe_ai/<year-month>.jsonl: the request, every
tool call and answer, and the outcome - what the model saw and did, for when a draft surprises.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from src.core import paths
from src.core.recipe_ai import knowledge
from src.core.recipe_ai.provider import AIError, make_provider
from src.core.recipe_ai.settings import Settings
from src.core.recipe_ai.tools import Proposal, Review, Toolbox
from src.core.recipe_check import check_recipe

log = logging.getLogger("recipe_ai")

LOG_DIR = os.path.join(paths.LOG_DIR, "recipe_ai")


@dataclass
class Outcome:
    mode: str
    request: str
    proposal: Optional[Proposal] = None
    review: Optional[Review] = None
    model: str = ""
    rounds: int = 0
    seconds: float = 0.0
    usage: dict = field(default_factory=dict)

    def cost_text(self) -> str:
        tokens = sum(self.usage.get(k, 0) for k in ("input", "output", "thinking"))
        return (f"{self.model}, {self.rounds} answer{'s' if self.rounds != 1 else ''}, {self.seconds:.0f} s"
                + (f", {tokens / 1000:.0f}k tokens" if tokens >= 1000 else ""))


def narrate(name: str, args: dict, catalog) -> str:
    """What the model is doing, for the builder's progress line."""
    args = args if isinstance(args, dict) else {}
    if name == "find_tags":
        return f"Looking up tags: “{str(args.get('query', '')).strip()}”"
    if name == "describe_tag":
        return f"Reading about the {catalog.label(str(args.get('tag', '')))}"
    if name == "read_help":
        page = str(args.get("page", ""))
        return f"Reading the help: {knowledge.HELP_PAGES.get(page, ('', page))[1]}"
    if name == "read_recipe":
        return f"Reading the recipe {str(args.get('file', '')).strip()}"
    if name == "check_recipe":
        return "Checking its draft"
    if name == "propose_recipe":
        return "Proposing its draft"
    if name == "submit_review":
        return "Writing up the review"
    return f"Using {name}"


def _answered(name: str, answer: dict) -> str:
    if "error" in answer:
        return f"{name}: {answer['error']}"
    if name == "check_recipe":
        return f"The checks: {answer.get('verdict', '')}"
    if name == "propose_recipe" and not answer.get("accepted"):
        n = len(answer.get("errors") or [])
        return f"Its draft has {n} error{'s' if n != 1 else ''}: sent back to be fixed"
    return ""


class Session:
    def __init__(self, mode: str, catalog, base: dict, filename: str = None, recipes_dir: str = None,
                 names: dict = None, settings: Settings = None, provider=None, progress=None, cancelled=None):
        self.mode = mode
        self.catalog = catalog
        self.base = json.loads(json.dumps(base, default=str))          # the operator's recipe, as it was asked about
        self.filename = filename
        self.recipes_dir = recipes_dir or paths.RECIPES_DIR
        self.recipe_path = os.path.join(self.recipes_dir, filename) if filename else None
        self.names = names or {}
        self.settings = settings or Settings.load()
        self.provider = provider
        self.progress = progress or (lambda text: None)
        self.cancelled = cancelled or (lambda: False)
        self.toolbox = Toolbox(mode, catalog, self.base, self.recipes_dir, self.recipe_path, self.names,
                               self.settings.max_proposals)
        self.chat = None
        self.pending = []           # the answers to the model's last calls, still to be sent (a refinement)
        self.request = ""

    # ------------------------------------------------------------------ what the builder asks for
    def draft(self, instruction: str, selected: tuple = None, earlier: dict = None, unticked: list = ()) -> Outcome:
        """earlier: a draft of the model's shown to the operator, to be changed as asked (a refinement
        whose conversation was lost); unticked: the changes of it they had left out."""
        problems = check_recipe(self.base, self.catalog, self.recipes_dir, self.recipe_path)
        text = knowledge.draft_task(instruction, self.base, problems, selected, self.filename, self.catalog,
                                    self.names, earlier, list(unticked))
        return self._run(instruction, text)

    def refine(self, instruction: str, unticked: list = ()) -> Outcome:
        if self.chat is None:
            raise AIError("refused", "There is no draft to refine.")
        self.toolbox.proposal = None
        self.toolbox.rejected = 0
        return self._run(instruction, knowledge.refine_task(instruction, list(unticked)))

    def review(self, focus: str = "") -> Outcome:
        problems = check_recipe(self.base, self.catalog, self.recipes_dir, self.recipe_path)
        text = knowledge.review_task(self.base, problems, self.filename, self.catalog, self.names, focus)
        return self._run(focus or "(review)", text)

    # ------------------------------------------------------------------ the conversation
    def _start(self):
        if self.chat is not None:
            return
        # (through the session: a refinement runs in another job, with its own progress line and cancel)
        provider = self.provider or make_provider(self.settings, lambda text: self.progress(text),
                                                  lambda: self.cancelled())
        self.provider = provider
        system = knowledge.system_prompt(self.catalog, self.mode, self.recipes_dir, self.filename, self.names)
        self.chat = provider.start(system, self.toolbox.specs())

    def _run(self, request: str, text: str) -> Outcome:
        started = time.monotonic()
        self.request = request
        outcome = Outcome(self.mode, request)
        first_call = len(self.toolbox.calls)
        error = None
        try:
            self._start()
            outcome.model = getattr(self.chat, "model", "")
            self._loop(text, outcome)
            return outcome
        except AIError as e:
            error = e
            raise
        except Exception as e:      # a fault here must reach the operator as a message, not end the builder
            log.exception("Recipe drafting failed")
            error = AIError("bad_reply", f"Drafting failed: {type(e).__name__}: {e}")
            raise error from None
        finally:
            outcome.seconds = time.monotonic() - started
            self._log(outcome, self.toolbox.calls[first_call:], error)

    def _send(self, outcome: Outcome, text: str = None, results: list = None):
        if outcome.rounds >= self.settings.max_rounds:
            raise AIError("gave_up", f"The model hadn't finished after {outcome.rounds} answers: try again, perhaps "
                                     "with a simpler request.")
        if self.cancelled():
            raise AIError("cancelled", "Cancelled.")
        self.progress(f"Waiting for {getattr(self.chat, 'model', 'the model')}")
        reply = self.chat.send(text=text, results=results)
        outcome.rounds += 1
        outcome.model = reply.model or outcome.model
        for k, v in (reply.usage or {}).items():
            outcome.usage[k] = outcome.usage.get(k, 0) + v
        return reply

    def _loop(self, text: str, outcome: Outcome):
        reply = self._send(outcome, text=text, results=self.pending or None)
        self.pending = []       # (only once they reached the model: after a failure they are sent next time)
        nudges = 0
        while True:
            if reply.calls:
                results = []
                for call in reply.calls:
                    if self.cancelled():
                        raise AIError("cancelled", "Cancelled.")
                    self.progress(narrate(call.name, call.args, self.catalog))
                    answer = self.toolbox.execute(call.name, call.args)
                    said = _answered(call.name, answer)
                    if said:
                        self.progress(said)
                    results.append((call, answer))
                if self.toolbox.finished:
                    self.pending = results          # answered first thing in a refinement
                    outcome.proposal, outcome.review = self.toolbox.proposal, self.toolbox.review
                    return
                reply = self._send(outcome, results=results)
                continue
            nudges += 1
            if nudges > 2:
                said = f": “{reply.text[:300]}”" if reply.text else ""
                raise AIError("gave_up", f"The model didn't finish{said}")
            finish = "propose_recipe" if self.mode == "draft" else "submit_review"
            if reply.finish == "MALFORMED_FUNCTION_CALL":
                nudge = ("Your last function call couldn't be read (its arguments weren't valid). Call it again: "
                         "recipe_json is the whole recipe as one JSON string.")
            elif reply.finish == "MAX_TOKENS":
                nudge = f"Your answer was cut off: too long. Call {finish} now, briefly."
            else:
                nudge = f"To finish, call {finish}: the operator sees only what you give it."
            reply = self._send(outcome, text=nudge)

    def _log(self, outcome: Outcome, calls: list, error):
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": self.mode, "request": outcome.request,
                  "file": self.filename, "model": outcome.model, "answers": outcome.rounds,
                  "seconds": round(outcome.seconds, 1), "tokens": outcome.usage, "calls": calls}
        if error is not None:
            record["error"] = {"kind": getattr(error, "kind", ""), "message": str(error)}
        if outcome.proposal is not None:
            record["proposal"] = {"summary": outcome.proposal.summary, "recipe": outcome.proposal.recipe,
                                  "assumptions": outcome.proposal.assumptions,
                                  "values_to_check": outcome.proposal.values_to_check}
        if outcome.review is not None:
            record["review"] = {"summary": outcome.review.summary,
                                "notes": [n.__dict__ for n in outcome.review.notes]}
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, time.strftime("%Y-%m") + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            log.warning("Recipe drafting: the session log can't be written: %s", e)
