"""Recipe conditions: a live value compared with a target.

    holds(value, "within", 15.0, tolerance=0.2)    # |value - 15| <= 0.2
    holds(value, ">=", 1.0)

One copy, for the steps (src/core/recipe_engine.py), the routines (src/core/routines.py), the
recipes' fault policies and the recipe checks. There used to be two, which disagreed on values
that aren't numbers (one raised, one didn't).
"""
import math

# operator -> how the recipe text says it
CONDITIONS = {
    "==": "is",
    "!=": "is not",
    ">": "is above",
    "<": "is below",
    ">=": "is at least",
    "<=": "is at most",
    "within": "is within",
}
EQUAL_TOLERANCE = 1e-6          # '==' on numbers: as near as this


def number(value):
    """The value as a float (true is 1, false is 0), or None if it isn't one."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def holds(current, condition: str, target, tolerance=None) -> bool:
    """True if the live value meets the condition. None (no live value), a value that isn't a
    number and an unknown operator never do."""
    c, t = number(current), number(target)
    if c is None or t is None:
        return False
    if condition == "==":
        return abs(c - t) < EQUAL_TOLERANCE
    if condition == "!=":
        return abs(c - t) >= EQUAL_TOLERANCE
    if condition == ">":
        return c > t
    if condition == "<":
        return c < t
    if condition == ">=":
        return c >= t
    if condition == "<=":
        return c <= t
    if condition == "within":
        tol = number(tolerance)
        return tol is not None and abs(c - t) <= tol
    return False


def problem(condition, target, tolerance=None) -> str:
    """Why a condition can't be evaluated ('' if it can): for the checks made when a recipe loads."""
    if condition not in CONDITIONS:
        return f"unknown condition {condition!r} (use one of {', '.join(CONDITIONS)})"
    if number(target) is None:
        return f"the value to compare with must be a number, not {target!r}"
    if condition == "within":
        tol = number(tolerance)
        if tol is None or tol < 0:
            return f"'within' needs a tolerance of 0 or more, not {tolerance!r}"
    return ""
