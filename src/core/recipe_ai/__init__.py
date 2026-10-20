"""Drafting and reviewing recipes with a language model, in the recipe builder.

The model never touches the machine. It proposes a recipe (or review notes); the proposal goes
through the same checks as any recipe (src/core/recipe_check.py) - one with errors is sent back
to the model to fix - and the operator sees it as changes to keep or discard. Nothing is saved or
run until they do, and saving and the sequencer check it again.

    settings.py     config/recipe_ai.json, and the API key (GEMINI_API_KEY, or the .env file)
    provider.py     the model service (Google Gemini, through google-genai), behind a small interface
    knowledge.py    what the model is told: the machine's tags, steps, procedures and templates,
                    made from the same catalogue the checks use, so the two can't disagree
    tools.py        what the model may do: look tags and help pages up, check a recipe, propose
    changes.py      what a proposal changes, change by change, and keeping some of them
    agent.py        a drafting or review session: the model and its tools, until it proposes
    cli.py          the same from a terminal (python -m src.core.recipe_ai.cli --help)

No Qt here: the recipe builder runs a session in a thread (src/gui/control/dialogs/recipe_builder/
drafting.py).
"""
from src.core.recipe_ai.provider import AIError
from src.core.recipe_ai.settings import Settings, availability

__all__ = ["AIError", "Settings", "availability"]
