"""Recipe drafting from a terminal: to see what the model is told, to try a tool, or to run a draft
without the HMI.

    python -m src.core.recipe_ai.cli check                          can this PC reach the model? (and how fast)
    python -m src.core.recipe_ai.cli prompt [--mode review]          the instructions the model gets
    python -m src.core.recipe_ai.cli task "add a 2 min wait" --recipe config/recipes/auto_tune.json --selected 3
    python -m src.core.recipe_ai.cli tool find_tags '{"query": "einzel current"}'
    python -m src.core.recipe_ai.cli draft "condition the einzel to 16 kV" [--recipe FILE] [--out FILE]
    python -m src.core.recipe_ai.cli review --recipe config/recipes/test_sequence.json

check, draft and review ask the model (they need the API key, see settings.py); the others don't. tool
runs one tool as the model would (its arguments as JSON, or @file for a file holding them); with
--state DIR a proposal or review it accepts is written there (proposal.json, review.json).
"""
import argparse
import json
import os
import sys

from src.core import paths
from src.core.recipe_ai import knowledge
from src.core.recipe_ai.changes import compute
from src.core.recipe_ai.provider import AIError
from src.core.recipe_ai.tools import Toolbox
from src.core.recipe_catalog import Catalog
from src.core.recipe_check import check_recipe
from src.core.recipe_store import read_recipe
from src.core.recipe_text import describe_policy_entry, describe_step


def _recipe(path: str):
    """(data, file name if it is in the recipes folder, else None)."""
    if not path:
        return {"recipe_name": "New recipe", "version": "1.0", "fault_policy": [], "steps": []}, None
    data, _ = read_recipe(path)
    inside = os.path.dirname(os.path.realpath(path)) == os.path.realpath(paths.RECIPES_DIR)
    return data, (os.path.basename(path) if inside else None)


def _selected(text: str):
    return tuple(int(x) - 1 for x in text.split(".")) if text else None


def _changes(base, proposed, catalog):
    return compute(base, proposed, lambda s: describe_step(s, catalog), lambda e: describe_policy_entry(e, catalog))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.core.recipe_ai.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="send the smallest request: can this PC reach the model, and how fast?")
    p = sub.add_parser("prompt", help="print the model's instructions")
    p.add_argument("--mode", choices=("draft", "review"), default="draft")
    for name in ("task", "draft"):
        p = sub.add_parser(name, help="print the request as the model gets it" if name == "task" else
                           "draft with the model")
        p.add_argument("request")
        p.add_argument("--recipe", default="")
        p.add_argument("--selected", default="", help="the selected step, e.g. 3 or 5.2")
        if name == "draft":
            p.add_argument("--out", default="", help="write the proposed recipe here")
    p = sub.add_parser("review", help="review a recipe with the model")
    p.add_argument("--recipe", required=True)
    p.add_argument("--focus", default="")
    p = sub.add_parser("tool", help="run one tool as the model would")
    p.add_argument("name")
    p.add_argument("args", nargs="?", default="{}")
    p.add_argument("--recipe", default="")
    p.add_argument("--mode", choices=("draft", "review"), default="draft")
    p.add_argument("--state", default="", help="write an accepted proposal or review here")
    opts = parser.parse_args(argv)

    if opts.command == "check":
        import logging
        from src.core.recipe_ai.provider import check_connection
        from src.core.recipe_ai.settings import Settings, availability
        logging.basicConfig(level=logging.INFO, format="  %(message)s")
        settings = Settings.load()
        ok, why = availability(settings)
        if not ok:
            print(f"Drafting isn't available: {why}", file=sys.stderr)
            return 1
        print(f"Asking {settings.model} (connection limit 15 s, answer limit {settings.timeout_s:.0f} s) ...")
        try:
            print(check_connection(settings))
        except AIError as e:
            print(f"Not connected: {e.message}", file=sys.stderr)
            return 1
        return 0

    catalog = Catalog.load()
    if opts.command == "prompt":
        sys.stdout.write(knowledge.system_prompt(catalog, opts.mode))
        return 0
    data, filename = _recipe(getattr(opts, "recipe", ""))
    recipe_path = os.path.join(paths.RECIPES_DIR, filename) if filename else None
    if opts.command == "task":
        problems = check_recipe(data, catalog, paths.RECIPES_DIR, recipe_path)
        print(knowledge.draft_task(opts.request, data, problems, _selected(opts.selected), filename, catalog))
        return 0
    if opts.command == "tool":
        box = Toolbox(opts.mode, catalog, data, paths.RECIPES_DIR, recipe_path)
        try:
            if opts.args.startswith("@"):               # @file: the arguments are in that file
                with open(opts.args[1:], "r", encoding="utf-8") as f:
                    args = json.load(f)
            else:
                args = json.loads(opts.args)
        except (OSError, ValueError) as e:
            print(json.dumps({"error": f"the arguments aren't JSON: {e}"}))
            return 2
        answer = box.execute(opts.name, args)
        print(json.dumps(answer, indent=1, ensure_ascii=False))
        if opts.state and box.proposal is not None:
            os.makedirs(opts.state, exist_ok=True)
            with open(os.path.join(opts.state, "proposal.json"), "w", encoding="utf-8") as f:
                json.dump({"recipe": box.proposal.recipe, "summary": box.proposal.summary,
                           "assumptions": box.proposal.assumptions, "values_to_check": box.proposal.values_to_check,
                           "problems": [p.text() for p in box.proposal.problems]}, f, indent=1, ensure_ascii=False)
        if opts.state and box.review is not None:
            os.makedirs(opts.state, exist_ok=True)
            with open(os.path.join(opts.state, "review.json"), "w", encoding="utf-8") as f:
                json.dump({"summary": box.review.summary, "notes": [n.__dict__ for n in box.review.notes]}, f,
                          indent=1, ensure_ascii=False)
        return 0

    from src.core.recipe_ai.agent import Session
    progress = (lambda text: print(f"  ... {text}", file=sys.stderr))
    try:
        if opts.command == "draft":
            session = Session("draft", catalog, data, filename, paths.RECIPES_DIR, progress=progress)
            outcome = session.draft(opts.request, _selected(opts.selected))
        else:
            session = Session("review", catalog, data, filename, paths.RECIPES_DIR, progress=progress)
            outcome = session.review(opts.focus)
    except AIError as e:
        print(f"Not done: {e.message}", file=sys.stderr)
        return 1
    print(f"({outcome.cost_text()})")
    if outcome.review is not None:
        print(outcome.review.summary or "(no summary)")
        for n in outcome.review.notes:
            print(f"- [{n.severity}] {n.where or 'the recipe'}: {n.text}" + (f"\n    fix: {n.fix}" if n.fix else ""))
        return 0
    proposal = outcome.proposal
    print(proposal.summary or "(no summary)")
    for c in _changes(data, proposal.recipe, catalog):
        print(f"* {c.title}")
        for line in c.was:
            print(f"    - {line}")
        for line in c.lines:
            print(f"    + {line}")
    for title, items in (("Assumed", proposal.assumptions), ("Check", proposal.values_to_check),
                         ("Problems", [p.text() for p in proposal.problems])):
        if items:
            print(f"{title}:")
            print("\n".join(f"  - {x}" for x in items))
    if opts.out:
        with open(opts.out, "w", encoding="utf-8") as f:
            json.dump(proposal.recipe, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"Written to {opts.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
