"""Settings for recipe drafting: config/recipe_ai.json (in git, so nothing secret goes in it), and
the API key, from the environment or the .env file in the SINDIE folder (not in git)."""
import importlib.util
import json
import logging
import os
import re
from dataclasses import dataclass, fields

from src.core import paths

log = logging.getLogger("recipe_ai")

CONFIG_FILE = "recipe_ai.json"
KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
KEY_HELP = ("add a line GEMINI_API_KEY=<your key> to the .env file in the SINDIE folder (keys: "
            "aistudio.google.com), then open the builder again")


@dataclass
class Settings:
    provider: str = "gemini"
    model: str = "gemini-3.8-flash"             # Google's newest stable Flash model (October 2026)
    # tried in turn if the model isn't found: an older stable one, then the alias for the newest Flash
    # (which can be a preview, with tighter rate limits - a last resort once these are retired)
    fallback_models: tuple = ("gemini-3.6-flash", "gemini-flash-latest")
    max_rounds: int = 16            # answers from the model in one draft, before it is given up
    max_proposals: int = 3          # proposals with errors sent back to be fixed; then the last is shown as it is
    timeout_s: float = 120.0        # for one answer from the service
    thinking_level: str = ""        # "": the model's own; "low", "medium" or "high" (Gemini 3 and later)
    temperature: float = None       # None: the model's own (Google advises leaving it for Gemini 3)
    enabled: bool = True            # false: the builder shows no drafting

    @classmethod
    def load(cls, path: str = None) -> "Settings":
        """The settings in config/recipe_ai.json, each one checked; the default for anything missing
        or wrong (logged)."""
        path = path or paths.config_path(CONFIG_FILE)
        settings = cls()
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            return settings
        except (OSError, ValueError) as e:
            log.warning("Recipe drafting: %s can't be read (%s): the defaults are used", path, e)
            return settings
        if not isinstance(data, dict):
            log.warning("Recipe drafting: %s isn't a JSON object: the defaults are used", path)
            return settings
        for f in fields(cls):
            if f.name not in data:
                continue
            value = _checked(f.name, data[f.name], getattr(settings, f.name))
            if value is _BAD:
                log.warning("Recipe drafting: %s in %s isn't valid (%r): the default %r is used", f.name, path,
                            data[f.name], getattr(settings, f.name))
            else:
                setattr(settings, f.name, value)
        return settings

    def describe(self) -> str:
        return f"Gemini, {self.model}" if self.provider == "gemini" else f"{self.provider}, {self.model}"


_BAD = object()


def _checked(name, value, default):
    if name in ("provider", "model", "thinking_level"):
        return value.strip() if isinstance(value, str) and (value.strip() or name == "thinking_level") else _BAD
    if name == "fallback_models":
        ok = isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
        return tuple(dict.fromkeys(v.strip() for v in value)) if ok else _BAD      # (each once)
    if name in ("max_rounds", "max_proposals"):
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100 else _BAD
    if name == "timeout_s":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 5 <= value <= 900
        return float(value) if ok else _BAD
    if name == "temperature":
        if value is None:
            return None
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 2
        return float(value) if ok else _BAD
    if name == "enabled":
        return value if isinstance(value, bool) else _BAD
    return _BAD


def _from_env_file(path: str) -> str:
    """The key in a .env file (KEY=value lines, as python-dotenv reads them), or ''."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() not in KEY_NAMES:
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):         # quoted: up to the closing quote (a comment may follow it)
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:                               # unquoted: a comment starts at a # after a space
            value = re.split(r"\s#", value, maxsplit=1)[0].strip()
        if value:
            return value
    return ""


def api_key() -> tuple:
    """(key, where it came from) - ('', '') if there is none. The key itself is never logged or
    shown: only where it came from."""
    for name in KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"the {name} environment variable"
    for folder in (paths.PROJECT_ROOT, os.getcwd()):
        path = os.path.join(folder, ".env")
        value = _from_env_file(path)
        if value:
            return value, path
    return "", ""


def availability(settings: Settings = None) -> tuple:
    """(True, 'Gemini, <model>') if drafting can be tried, else (False, why not - in words for the
    operator). Nothing is sent anywhere: whether the service answers is found out on the first draft."""
    settings = settings or Settings.load()
    if not settings.enabled:
        return False, f"Drafting is switched off (enabled is false in config/{CONFIG_FILE})."
    if settings.provider != "gemini":
        return False, f"Unknown provider {settings.provider!r} in config/{CONFIG_FILE} (only gemini is supported)."
    try:            # (only whether it is there: importing it takes a while, and is left to the first draft)
        found = importlib.util.find_spec("google.genai") is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        return False, "The google-genai package isn't installed: run pip install -r requirements.txt."
    if not api_key()[0]:
        return False, f"No API key for Gemini: {KEY_HELP}."
    return True, settings.describe()
