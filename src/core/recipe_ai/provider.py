"""The language model behind recipe drafting, behind a small interface:

    provider = make_provider(settings, progress, cancelled)
    session = provider.start(system_prompt, tool_specs)      # tool_specs: tools.Toolbox.specs()
    reply = session.send(text="...")                          # Reply(text, calls, usage, model, finish)
    reply = session.send(results=[(call, {...}), ...])        # the tools' answers to reply.calls

Google Gemini through google-genai is the one provider. The session keeps the whole conversation,
as the service returned it: Gemini 3 and later models sign their function calls, and the
signatures must go back unchanged.

Every failure is an AIError whose message is meant for the operator ('The API key was refused
...'), never a traceback. The key is never logged or put in a message.
"""
import importlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("recipe_ai")


class AIError(Exception):
    """kind: no_package, no_key, auth, model, quota, network, timeout, server, refused, blocked,
    bad_reply, cancelled, gave_up."""

    def __init__(self, kind: str, message: str, retry_after: float = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


@dataclass
class Call:
    name: str
    args: dict
    id: str = ""


@dataclass
class Reply:
    text: str = ""
    calls: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)       # input, output, thinking, cached (tokens)
    model: str = ""
    finish: str = ""                                # STOP, MALFORMED_FUNCTION_CALL, MAX_TOKENS ...


def wait(seconds: float, cancelled=None):
    """Sleep, waking every 0.1 s to see if the draft was cancelled (AIError 'cancelled')."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancelled is not None and cancelled():
            raise AIError("cancelled", "Cancelled.")
        time.sleep(min(0.1, max(0.0, end - time.monotonic())))


_RETRY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def classify(e: Exception, model: str) -> AIError:
    """An AIError, in words for the operator, for whatever the service or the network raised."""
    if isinstance(e, AIError):
        return e
    try:
        import httpx
    except ImportError:          # (google-genai brings httpx: without it nothing gets this far)
        httpx = None
    try:
        from google.genai import errors as genai_errors
    except ImportError:
        genai_errors = None
    if genai_errors is not None and isinstance(e, genai_errors.APIError):
        code = int(getattr(e, "code", 0) or 0)
        status = str(getattr(e, "status", "") or "")
        message = str(getattr(e, "message", "") or "").strip()
        details = json.dumps(getattr(e, "details", None) or {}, default=str)
        said = f" ({code} {status}: {message[:200]})" if message else f" ({code} {status})"
        if code in (401, 403) or "API_KEY_INVALID" in details or "api key not valid" in message.lower():
            return AIError("auth", "The service refused the API key" + said + ": check GEMINI_API_KEY in the .env "
                                   "file.")
        if code == 404:
            return AIError("model", f"The model {model!r} isn't available to this key{said}: set another in "
                                    "config/recipe_ai.json.")
        if code == 429:
            found = _RETRY.search(details)
            return AIError("quota", "The service's rate limit or quota for this key is used up" + said + ": try again "
                                    "in a minute.", retry_after=float(found.group(1)) if found else None)
        if code >= 500:
            return AIError("server", f"The service is busy or failing{said}: try again shortly.", retry_after=0.0)
        return AIError("refused", f"The service refused the request{said}.")
    if httpx is not None and isinstance(e, httpx.TimeoutException):
        return AIError("timeout", "No answer from the service in time: try again, or set a longer timeout_s in "
                                  "config/recipe_ai.json.")
    if (httpx is not None and isinstance(e, httpx.HTTPError)) or isinstance(e, OSError):
        return AIError("network", f"Can't reach the service ({type(e).__name__}: {str(e)[:200]}): is this PC "
                                  "online?")
    return AIError("bad_reply", f"Unexpected error from the service: {type(e).__name__}: {str(e)[:300]}")


def _name(value) -> str:
    """'STOP' for FinishReason.STOP (an enum, or already a string)."""
    if value is None:
        return ""
    return str(getattr(value, "name", value)).rpartition(".")[2]


class GeminiSession:
    def __init__(self, provider: "GeminiProvider", system: str, tools: list):
        from google.genai import types
        self.p = provider
        self.types = types
        self.contents = []
        self.answered = False           # the model has answered: no more falling back to another model
        declarations = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                                  parameters_json_schema=t["parameters"]) for t in tools]
        config = dict(system_instruction=system,
                      tools=[types.Tool(function_declarations=declarations)] if declarations else None,
                      automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        if provider.settings.thinking_level:
            config["thinking_config"] = types.ThinkingConfig(thinking_level=provider.settings.thinking_level)
        if provider.settings.temperature is not None:
            config["temperature"] = provider.settings.temperature
        self.config = types.GenerateContentConfig(**config)

    @property
    def model(self) -> str:
        return self.p.model

    def send(self, text: str = None, results: list = None) -> Reply:
        """The operator's words, or the tools' answers ([(Call, dict)]) - then the model's reply."""
        types = self.types
        parts = []
        for call, result in results or []:
            parts.append(types.Part(function_response=types.FunctionResponse(id=call.id or None, name=call.name,
                                                                             response=result)))
        if text:
            parts.append(types.Part(text=text))
        if self.contents and self.contents[-1].role == "user" and not results:
            # (the model's last answer was empty: say more in the same turn, so turns still alternate)
            added, had = self.contents[-1], len(self.contents[-1].parts or [])
            added.parts = list(added.parts or []) + parts
        else:
            added, had = types.Content(role="user", parts=parts), None
            self.contents.append(added)
        try:
            return self._generate()
        except AIError:
            # the session can be carried on (a refinement after a failure): take back what was added
            if self.contents and self.contents[-1] is added:
                if had is None:
                    self.contents.pop()
                else:
                    added.parts = added.parts[:had]
            raise

    def _generate(self) -> Reply:
        delays = [2.0, 6.0]
        while True:
            self.p.check_cancelled()
            try:
                response = self.p.client.models.generate_content(model=self.p.model, contents=self.contents,
                                                                 config=self.config)
            except Exception as e:      # (whatever the SDK or the network raises: classified for the operator)
                err = classify(e, self.p.model)
                if err.kind == "model" and not self.answered and self.p.next_model():
                    log.info("Recipe drafting: %s", err.message)
                    self.p.progress(f"Trying the model {self.p.model}")
                    continue
                if err.kind in ("server", "quota") and delays:
                    pause = max(delays.pop(0), err.retry_after or 0.0)    # (each retry uses one up)
                    if pause <= 30.0:
                        self.p.progress(f"The service is busy: trying again in {pause:.0f} s")
                        wait(pause, self.p.cancelled)
                        continue
                raise err from None
            return self._reply(response)

    def _reply(self, response) -> Reply:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            feedback = getattr(response, "prompt_feedback", None)
            reason = _name(getattr(feedback, "block_reason", None)) or "no reason given"
            raise AIError("blocked", f"The service gave no answer ({reason}).")
        candidate = candidates[0]
        finish = _name(getattr(candidate, "finish_reason", None))
        content = getattr(candidate, "content", None)
        parts = list(getattr(content, "parts", None) or [])
        if not parts and finish in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
            raise AIError("blocked", f"The service withheld its answer ({finish}).")
        if parts:
            self.contents.append(content)       # as returned: its function calls' signatures go back unchanged
            self.answered = True
        text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
        calls = []
        for p in parts:
            fc = getattr(p, "function_call", None)
            if fc is not None and fc.name:
                calls.append(Call(fc.name, dict(fc.args or {}), fc.id or ""))
        meta = getattr(response, "usage_metadata", None)
        usage = {k: int(getattr(meta, attr, 0) or 0) for k, attr in (
            ("input", "prompt_token_count"), ("output", "candidates_token_count"),
            ("thinking", "thoughts_token_count"), ("cached", "cached_content_token_count"))} if meta else {}
        return Reply(text=text.strip(), calls=calls, usage=usage,
                     model=str(getattr(response, "model_version", "") or self.p.model), finish=finish)


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings, key: str, progress=None, cancelled=None, http_client=None):
        from google import genai
        from google.genai import types
        self.settings = settings
        self.models = list(dict.fromkeys([settings.model, *settings.fallback_models]))     # (each once, in order)
        self._next = 1
        self.model = self.models[0]
        self.progress = progress or (lambda text: None)
        self.cancelled = cancelled or (lambda: False)
        options = dict(timeout=int(settings.timeout_s * 1000))
        if http_client is not None:          # (the tests answer for the service)
            options["httpx_client"] = http_client
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(**options))

    def next_model(self) -> bool:
        """On to the next model of the list (each is tried once); False when there are no more."""
        if self._next < len(self.models):
            self.model = self.models[self._next]
            self._next += 1
            return True
        return False

    def check_cancelled(self):
        if self.cancelled():
            raise AIError("cancelled", "Cancelled.")

    def start(self, system: str, tools: list) -> GeminiSession:
        return GeminiSession(self, system, tools)


def make_provider(settings, progress=None, cancelled=None, http_client=None):
    """The provider the settings name, ready to start sessions. AIError (no_package, no_key, refused)
    if it can't be."""
    from src.core.recipe_ai.settings import KEY_HELP, api_key
    if settings.provider != "gemini":
        raise AIError("refused", f"Unknown provider {settings.provider!r} in config/recipe_ai.json (only gemini).")
    try:
        importlib.import_module("google.genai")
    except ImportError:
        raise AIError("no_package", "The google-genai package isn't installed: run pip install -r "
                                    "requirements.txt.") from None
    key, _where = api_key()
    if not key:
        raise AIError("no_key", f"No API key for Gemini: {KEY_HELP}.")
    return GeminiProvider(settings, key, progress, cancelled, http_client)
