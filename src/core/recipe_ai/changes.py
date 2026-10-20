"""What a proposal changes in a recipe, change by change, so the operator can keep some of the
changes and not others:

    changes = compute(base, proposed, describe)      # [Change], in the order of the recipe
    result = apply(base, proposed, changes, kept)     # the base with the kept changes (by id)
    rows = merged(base, proposed, changes)            # every step of both, for the list of steps

Steps are compared as the sequencer sees them: true and 1 are the same value, the order of a step's
fields and its number don't matter. Whatever a change doesn't touch is kept exactly as it was in the
base - its fields, its spelling - so a proposal that rewrites a step without changing it changes
nothing.
"""
import copy
import difflib
import json
from dataclasses import dataclass
from typing import Optional

from src.core.recipe_store import renumber

STEP_KINDS = ("add", "remove", "change", "replace")


@dataclass(frozen=True)
class Change:
    id: int
    kind: str               # add, remove, change, replace (steps); stops; name; description
    base: tuple = ()        # (i1, i2): the base's steps (or stop conditions) it takes out
    new: tuple = ()         # (j1, j2): the proposal's steps (or stop conditions) it puts in
    title: str = ""         # "Add 2 steps after step 3", "Change step 5", "Stop conditions: add" ...
    lines: tuple = ()       # what it puts in, in words
    was: tuple = ()         # what it takes out, in words


@dataclass(frozen=True)
class Row:
    """A step in the list while a proposal is shown: from the base or from the proposal."""
    source: str             # "base" or "new"
    index: int              # its place in that recipe's steps
    change: Optional[int] = None     # the change it is part of (None: unchanged)
    kind: str = ""          # add, remove or change ('' unchanged)


def canonical(value):
    """For comparing: numbers (and true/false) as floats, no step numbers, keys in order."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items()) if k != "step_id"}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def key(value) -> str:
    return json.dumps(canonical(value), sort_keys=True, ensure_ascii=False, default=str)


def same(a: dict, b: dict) -> bool:
    """The same recipe, as the sequencer sees it (name, description, stop conditions, steps)."""
    fields = ("recipe_name", "description", "fault_policy", "steps")
    norm = (lambda d: {f: (d.get(f) or ([] if f in ("fault_policy", "steps") else "")) for f in fields})
    return key(norm(a)) == key(norm(b))


def _steps(data) -> list:
    steps = data.get("steps") if isinstance(data, dict) else None
    return steps if isinstance(steps, list) else []


def _policy(data) -> list:
    policy = data.get("fault_policy") if isinstance(data, dict) else None
    return policy if isinstance(policy, list) else []


def _kind(step) -> str:
    """What kind of step it is: its type, and for a procedure which one."""
    if not isinstance(step, dict):
        return "?"
    kind = str(step.get("type"))
    return f"{kind}:{step.get('routine_name')}" if kind == "ROUTINE_CALL" else kind


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _numbers(i1: int, i2: int) -> str:
    return f"step {i1 + 1}" if i2 - i1 == 1 else f"steps {i1 + 1} to {i2}"


def compute(base: dict, proposed: dict, describe=None, describe_entry=None) -> list:
    """The changes from base to proposed: its name, its description, its steps (each changed step
    on its own, a run of added or removed steps as one), its stop conditions."""
    describe = describe or (lambda s: str(s.get("type", "?")) if isinstance(s, dict) else "(not a step)")
    describe_entry = describe_entry or (lambda e: str(e.get("name") or e.get("target") or "?")
                                        if isinstance(e, dict) else "(not a stop condition)")
    out = []

    def add(**kw):
        out.append(Change(id=len(out), **kw))

    for field, kind, label in (("recipe_name", "name", "Name"), ("description", "description", "About")):
        a, b = str(base.get(field) or "").strip(), str(proposed.get(field) or "").strip()
        if a != b:
            add(kind=kind, title=f"{label}: {b or '(none)'}", lines=(b,) if b else (), was=(a,) if a else ())

    a, b = _steps(base), _steps(proposed)

    def steps_change(tag, i1, i2, j1, j2):
        if tag == "insert":
            where = f" after step {i1}" if i1 else (" at the start" if a else "")
            add(kind="add", base=(i1, i1), new=(j1, j2), title=f"Add {_plural(j2 - j1, 'step')}{where}",
                lines=tuple(describe(s) for s in b[j1:j2]))
        elif tag == "delete":
            add(kind="remove", base=(i1, i2), new=(j1, j1), title=f"Remove {_numbers(i1, i2)}",
                was=tuple(describe(s) for s in a[i1:i2]))
        else:
            add(kind="replace", base=(i1, i2), new=(j1, j2),
                title=f"Replace {_numbers(i1, i2)} with {_plural(j2 - j1, 'step')}",
                lines=tuple(describe(s) for s in b[j1:j2]), was=tuple(describe(s) for s in a[i1:i2]))

    matcher = difflib.SequenceMatcher(a=[key(s) for s in a], b=[key(s) for s in b], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace":
            steps_change(tag, i1, i2, j1, j2)
            continue
        # A run of changed steps: lined up by their kind, so a step changed beside one added reads as a
        # change and an addition, not as one replaced by two
        kinds = difflib.SequenceMatcher(a=[_kind(s) for s in a[i1:i2]], b=[_kind(s) for s in b[j1:j2]],
                                        autojunk=False)
        for sub, x1, x2, y1, y2 in kinds.get_opcodes():
            if sub != "equal":
                steps_change(sub, i1 + x1, i1 + x2, j1 + y1, j1 + y2)
                continue
            for k in range(x2 - x1):
                i, j = i1 + x1 + k, j1 + y1 + k
                if key(a[i]) != key(b[j]):
                    add(kind="change", base=(i, i + 1), new=(j, j + 1), title=f"Change step {i + 1}",
                        lines=(describe(b[j]),), was=(describe(a[i]),))

    pa, pb = _policy(base), _policy(proposed)
    matcher = difflib.SequenceMatcher(a=[key(e) for e in pa], b=[key(e) for e in pb], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        verb = {"insert": "add", "delete": "remove"}.get(tag, "change")
        add(kind="stops", base=(i1, i2), new=(j1, j2), title=f"Stop conditions: {verb}",
            lines=tuple(describe_entry(e) for e in pb[j1:j2]), was=tuple(describe_entry(e) for e in pa[i1:i2]))
    return out


def _merge(a: list, b: list, changes: list, kept: set) -> list:
    out, pos = [], 0
    for c in changes:
        out.extend(copy.deepcopy(a[pos:c.base[0]]))
        out.extend(copy.deepcopy(b[c.new[0]:c.new[1]] if c.id in kept else a[c.base[0]:c.base[1]]))
        pos = c.base[1]
    out.extend(copy.deepcopy(a[pos:]))
    return out


def apply(base: dict, proposed: dict, changes: list, kept) -> dict:
    """The base recipe with the kept changes (ids) made, the steps numbered again. The base itself
    isn't changed."""
    kept = set(kept)
    out = copy.deepcopy(base)
    for c in changes:
        if c.id not in kept:
            continue
        if c.kind == "name":
            out["recipe_name"] = str(proposed.get("recipe_name") or "")
        elif c.kind == "description":
            text = str(proposed.get("description") or "").strip()
            if text:
                out["description"] = proposed.get("description")
            else:
                out.pop("description", None)
    out["steps"] = _merge(_steps(base), _steps(proposed), [c for c in changes if c.kind in STEP_KINDS], kept)
    out["fault_policy"] = _merge(_policy(base), _policy(proposed), [c for c in changes if c.kind == "stops"], kept)
    renumber(out["steps"])
    return out


def merged(base: dict, proposed: dict, changes: list) -> list:
    """[Row] for every step of the base and of the proposal, in order: what is unchanged, what a
    change takes out (from the base) and what it puts in (from the proposal); a changed step once,
    as proposed."""
    rows, pos = [], 0
    for c in changes:
        if c.kind not in STEP_KINDS:
            continue
        rows.extend(Row("base", i) for i in range(pos, c.base[0]))
        if c.kind == "change":
            rows.append(Row("new", c.new[0], c.id, "change"))
        else:
            rows.extend(Row("base", i, c.id, "remove") for i in range(*c.base))
            rows.extend(Row("new", j, c.id, "add") for j in range(*c.new))
        pos = c.base[1]
    rows.extend(Row("base", i) for i in range(pos, len(_steps(base))))
    return rows


def present(row: Row, kept) -> bool:
    """Whether the row's step is in the result, with the changes kept."""
    if row.change is None or row.kind == "change":
        return True
    return (row.change in kept) == (row.kind == "add")


def numbers(rows: list, kept) -> list:
    """The number each row's step has in the result (None: it won't be in it)."""
    out, n = [], 0
    for row in rows:
        if present(row, kept):
            n += 1
            out.append(n)
        else:
            out.append(None)
    return out
