"""One gateway for every LLM call, with free-model fallback through OpenRouter.

Four places call a language model: the chat answer (`api/rag.py`), the report's
interpretive sections (`api/report.py`), the relevance QC judge (`qc.py`) and AI
review extraction (`connectors/scrapegraph_source.py`). Each used to build its
own OpenAI client, so when the OpenAI account ran out of quota every one of them
failed, and each degraded on its own terms.

They now all go through `complete()`, which walks a list of attempts:

1. OpenAI, exactly as each call site used it before (same model, same API, same
   parameters), when OPENAI_API_KEY is set;
2. then OpenRouter, one model at a time from OPENROUTER_MODELS, when
   OPENROUTER_API_KEY is set and the task is in OPENROUTER_TASKS.

The default model list ends with `openrouter/free`, OpenRouter's own router over
whichever free models exist that day. Free models come and go (two of the
zero-priced entries in OpenRouter's catalogue when this was written were music
generators billed per song), so a fixed list rots; a router at the end does not.

**Cooldowns.** A provider that is out of quota will be out of quota on the next
request too, so failures are remembered: insufficient quota, missing credits and
bad keys put the whole provider aside for 30 minutes; a rate limit puts aside
that one model for as long as the server's Retry-After says (or a growing back-
off when it does not say); a model that no longer exists is set aside for hours.
Transient failures (5xx, timeouts) are not remembered — the next request simply
tries again. Without this, every chat message would first wait on a provider
that is known to be refusing.

**Why OPENROUTER_TASKS defaults to chat and report.** A free OpenRouter account
gets 50 free-model requests a DAY (1000 once $10 of credit has ever been bought).
One QC pass is up to 24 requests, so letting the background QC loop fall back to
free models would spend the whole day's allowance in two cycles and leave the
chat — the thing a person is actually waiting on — with nothing. Set
OPENROUTER_TASKS=all once the account has credit.

**What this does not do: change embeddings.** Vectors from different embedding
models live in different spaces, so falling back to a "free embedding model"
would make search silently return nonsense. See `embeddings.py`, which falls
back only to the SAME model served through OpenRouter.

Nothing here logs a key. Failure messages are the providers' own, truncated.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)

TASKS = ("chat", "report", "qc", "extraction")

OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

#: Tried in order after OpenAI. Chosen from OpenRouter's catalogue on 2026-10-20
#: for being general-purpose, supporting JSON output, and having long contexts;
#: `openrouter/free` last so the list keeps working as the catalogue changes.
DEFAULT_OPENROUTER_MODELS = (
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/free",
)

#: How long each kind of failure sets a provider or model aside, in seconds.
_COOLDOWN = {
    "quota": 30 * 60,
    "credits": 30 * 60,
    "auth": 30 * 60,
    "daily_limit": 60 * 60,
    "model_unavailable": 6 * 60 * 60,
    "data_policy": 6 * 60 * 60,
}
_RATE_LIMIT_BASE = 30
_RATE_LIMIT_MAX = 30 * 60
#: Failures that mean the whole provider is unusable, not just one model.
_PROVIDER_WIDE = frozenset({"quota", "credits", "auth", "daily_limit"})


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def openrouter_models() -> list[str]:
    return _env_list("OPENROUTER_MODELS") or list(DEFAULT_OPENROUTER_MODELS)


def openrouter_tasks() -> set[str]:
    raw = _env_list("OPENROUTER_TASKS") or ["chat", "report"]
    if any(t.lower() == "all" for t in raw):
        return set(TASKS)
    return {t.lower() for t in raw}


def is_free_model(model: str) -> bool:
    m = (model or "").strip().lower()
    return m.endswith(":free") or m == "openrouter/free"


def _primary() -> str:
    return os.environ.get("LLM_PRIMARY", "auto").strip().lower()


def is_reasoning_model(model: str) -> bool:
    """reasoning={effort} is only valid for OpenAI reasoning models (o-series,
    gpt-5); sending it to a standard model 400s."""
    m = (model or "").lower().split("/")[-1]
    return m.startswith(("o1", "o3", "o4")) or "gpt-5" in m


# --- results and errors ---------------------------------------------------------


@dataclass
class Completion:
    text: str
    provider: str            # "openai" | "openrouter"
    model: str               # the model that ACTUALLY answered
    requested_model: str     # what was asked for (differs for openrouter/free)
    fallback: bool           # True when an earlier attempt was skipped or failed
    usage: dict = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)

    def label(self) -> dict:
        """What the UI shows about who answered."""
        return {
            "provider": self.provider,
            "model": self.model,
            "fallback": self.fallback,
            "free": self.provider == "openrouter" and is_free_model(self.requested_model),
        }


class LLMUnavailable(RuntimeError):
    """No configured provider produced an answer."""

    def __init__(self, message: str, failures: list[dict] | None = None):
        super().__init__(message)
        self.failures = failures or []


@dataclass(frozen=True)
class _Attempt:
    provider: str
    model: str


# --- state ----------------------------------------------------------------------

_LOCK = threading.Lock()
_COOLING: dict[tuple[str, str, str], tuple[float, str]] = {}   # key -> (until, kind)
_STRIKES: dict[tuple[str, str], int] = {}
_STATS: dict[tuple[str, str], dict] = {}
_WARNED_PAID: set[str] = set()


def _stat(attempt: _Attempt) -> dict:
    return _STATS.setdefault(
        (attempt.provider, attempt.model),
        {"attempts": 0, "successes": 0, "failures": {}, "last_error": None, "last_ok": None},
    )


def _cool_keys(attempt: _Attempt, mode: str) -> tuple[tuple[str, str, str], ...]:
    return ((attempt.provider, "*", "*"), (attempt.provider, attempt.model, mode))


def _cooling_until(attempt: _Attempt, mode: str, now: float) -> tuple[float, str] | None:
    with _LOCK:
        for key in _cool_keys(attempt, mode):
            hit = _COOLING.get(key)
            if hit and hit[0] > now:
                return hit
    return None


def reset_state() -> None:
    """Forget cooldowns, counters and cached clients (tests, or after fixing a key)."""
    with _LOCK:
        _COOLING.clear()
        _STRIKES.clear()
        _STATS.clear()
        _CLIENTS.clear()
        _WARNED_PAID.clear()


# --- planning -------------------------------------------------------------------


def _plan(task: str, openai_model: str | None) -> list[_Attempt]:
    openai = []
    if os.environ.get("OPENAI_API_KEY") and openai_model:
        openai = [_Attempt("openai", openai_model)]
    router = []
    if os.environ.get("OPENROUTER_API_KEY") and task in openrouter_tasks():
        router = [_Attempt("openrouter", m) for m in openrouter_models()]
        for m in openrouter_models():
            if not is_free_model(m) and m not in _WARNED_PAID:
                _WARNED_PAID.add(m)
                log.warning("llm: OPENROUTER_MODELS includes %s, which is not a free model; "
                            "requests that fall back to it are billed", m)
    if _primary() == "openrouter":
        return router + openai
    return openai + router


def available(task: str, openai_model: str | None = "default") -> bool:
    """Whether any provider is configured for this task (cooldowns aside)."""
    return bool(_plan(task, openai_model))


# --- clients --------------------------------------------------------------------

_CLIENTS: dict[tuple, object] = {}


def _make_client(provider: str):
    from openai import OpenAI  # type: ignore

    if provider == "openrouter":
        headers = {"X-Title": "ReviewBay"}
        site = os.environ.get("OPENROUTER_SITE_URL", "").strip()
        if site:
            headers["HTTP-Referer"] = site
        # No SDK retries: a refusing model is a reason to try the NEXT model,
        # not to wait on the same one.
        return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"],
                      max_retries=0, default_headers=headers)
    # One quick retry for a transient blip on the primary; an exhausted quota is
    # caught by the cooldown after that, so it costs at most one extra request.
    return OpenAI(max_retries=1)


def _client(provider: str):
    key = (
        provider,
        os.environ.get("OPENROUTER_API_KEY" if provider == "openrouter" else "OPENAI_API_KEY"),
        OPENROUTER_BASE_URL if provider == "openrouter" else None,
    )
    with _LOCK:
        client = _CLIENTS.get(key)
    if client is None:
        client = _make_client(provider)
        with _LOCK:
            _CLIENTS[key] = client
    return client


# --- one call -------------------------------------------------------------------


def responses_text(resp) -> str:
    """The text of a Responses API result, however the SDK shaped it."""
    text = getattr(resp, "output_text", None)
    if text:
        return str(text)
    parts = []
    for item in getattr(resp, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            value = getattr(content, "text", None)
            if value:
                parts.append(str(value))
    return "".join(parts)


def _call(attempt: _Attempt, *, system: str, user: str, json_mode: bool,
          max_tokens: int | None, temperature: float | None, openai_api: str,
          timeout: float) -> tuple[str, str, dict]:
    client = _client(attempt.provider).with_options(timeout=timeout)
    model = attempt.model

    if attempt.provider == "openai" and openai_api == "responses":
        # Exactly what rag.py and report.py sent before the gateway existed.
        kwargs = dict(model=model, instructions=system, input=user)
        if max_tokens:
            kwargs["max_output_tokens"] = max_tokens
        if is_reasoning_model(model):
            kwargs["reasoning"] = {"effort": os.environ.get("OPENAI_REASONING_EFFORT", "low")}
        resp = client.responses.create(**kwargs)
        usage = getattr(resp, "usage", None)
        return responses_text(resp), getattr(resp, "model", None) or model, {
            "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }

    kwargs = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    if attempt.provider == "openrouter":
        # Free models include reasoning models that spend tokens thinking before
        # they answer, so give them room; an empty answer counts as a failure.
        kwargs["max_tokens"] = max(max_tokens or 0, 4096)
        if os.environ.get("OPENROUTER_DATA_COLLECTION", "").strip().lower() == "deny":
            kwargs["extra_body"] = {"provider": {"data_collection": "deny"}}
    elif max_tokens:
        kwargs["max_completion_tokens" if is_reasoning_model(model) else "max_tokens"] = max_tokens
    if temperature is not None and not (attempt.provider == "openai" and is_reasoning_model(model)):
        kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    choices = getattr(resp, "choices", None) or []
    text = (choices[0].message.content or "") if choices else ""
    usage = getattr(resp, "usage", None)
    return text, getattr(resp, "model", None) or model, {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


# --- failure handling -------------------------------------------------------------


# How a provider says "this account cannot pay". OpenAI sends it as a 429, the
# same status as a rate limit, which is why it has to be recognised: retrying it
# changes nothing. Production returned code "credit_balance_exhausted" with type
# "insufficient_quota"; older responses put "insufficient_quota" in the code.
# Matching only the code missed the newer form and treated an empty account as a
# brief rate limit.
_OUT_OF_CREDIT_CODES = frozenset({
    "insufficient_quota", "credit_balance_exhausted",
    "billing_hard_limit_reached", "billing_not_active",
})
_OUT_OF_CREDIT_PHRASES = (
    "insufficient_quota", "exceeded your current quota", "no credits remaining",
    "credit balance", "billing hard limit",
)


def is_out_of_credit(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "").lower()
    kind = str(getattr(exc, "type", "") or "").lower()
    message = (str(getattr(exc, "message", "") or "") + " " + str(exc)).lower()
    return (
        code in _OUT_OF_CREDIT_CODES
        or kind in _OUT_OF_CREDIT_CODES
        or any(phrase in message for phrase in _OUT_OF_CREDIT_PHRASES)
    )


def classify(exc: BaseException, provider: str) -> str:
    """Name the kind of failure, which decides whether and how long to back off."""
    status = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "").lower()
    message = str(getattr(exc, "message", "") or exc).lower()
    name = type(exc).__name__

    if status == 401 or name == "AuthenticationError":
        return "auth"
    if status == 402:
        return "credits"
    if status == 403 or name == "PermissionDeniedError":
        # OpenRouter answers 403 when a prompt trips a provider's moderation:
        # that is about this request, not the model.
        return "moderation" if ("moderation" in message or "flagged" in message) else "auth"
    if status == 429 or name == "RateLimitError":
        if is_out_of_credit(exc):
            return "quota"
        if provider == "openrouter" and ("per-day" in message or "per day" in message):
            return "daily_limit"
        return "rate_limit"
    if status == 404 or name == "NotFoundError":
        return "data_policy" if "data policy" in message else "model_unavailable"
    if status == 400 or name == "BadRequestError":
        if "context" in message and ("length" in message or "window" in message or "token" in message):
            return "context"
        return "bad_request"
    if name in ("APITimeoutError", "APIConnectionError", "TimeoutError"):
        return "unreachable"
    if (isinstance(status, int) and status >= 500) or name == "InternalServerError":
        return "server"
    return "error"


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    reset = headers.get("x-ratelimit-reset") if hasattr(headers, "get") else None
    if reset:
        try:
            # OpenRouter sends a millisecond epoch here.
            return max(0.0, float(reset) / 1000.0 - time.time())
        except ValueError:
            pass
    return None


def _record_failure(attempt: _Attempt, mode: str, kind: str, exc: BaseException | None,
                    now: float) -> None:
    message = (str(getattr(exc, "message", "") or exc) if exc is not None else kind)[:200]
    with _LOCK:
        stat = _stat(attempt)
        stat["attempts"] += 1
        stat["failures"][kind] = stat["failures"].get(kind, 0) + 1
        stat["last_error"] = {"kind": kind, "message": message, "at": now}

        seconds = 0.0
        if kind == "rate_limit":
            strikes = _STRIKES.get((attempt.provider, attempt.model), 0)
            _STRIKES[(attempt.provider, attempt.model)] = strikes + 1
            hinted = _retry_after(exc) if exc is not None else None
            seconds = min(_RATE_LIMIT_MAX, hinted if hinted else _RATE_LIMIT_BASE * (2 ** strikes))
        elif kind in _COOLDOWN:
            seconds = _COOLDOWN[kind]
            if kind == "daily_limit" and exc is not None:
                seconds = min(24 * 3600, _retry_after(exc) or seconds)
        if seconds > 0:
            if kind in _PROVIDER_WIDE:
                key = (attempt.provider, "*", "*")
            else:
                key = (attempt.provider, attempt.model, mode)
            _COOLING[key] = (now + seconds, kind)

    level = logging.ERROR if kind == "auth" else logging.WARNING
    hint = ""
    if kind == "data_policy":
        hint = (" — OpenRouter refused this free model under the account's privacy "
                "settings; see https://openrouter.ai/settings/privacy")
    elif kind == "quota":
        hint = " — the account is out of quota; falling back where configured"
    elif kind == "daily_limit":
        hint = " — the free-model daily allowance is used up"
    log.log(level, "llm: %s %s failed (%s): %s%s",
            attempt.provider, attempt.model, kind, message, hint)


def _record_success(attempt: _Attempt, now: float) -> None:
    with _LOCK:
        stat = _stat(attempt)
        stat["attempts"] += 1
        stat["successes"] += 1
        stat["last_ok"] = now
        _STRIKES.pop((attempt.provider, attempt.model), None)


# --- the entry point ----------------------------------------------------------------


def complete(
    task: str,
    *,
    system: str,
    user: str,
    json_mode: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
    openai_model: str | None = None,
    openai_api: str = "chat",
    timeout: float | None = None,
    deadline: float | None = None,
) -> Completion:
    """Answer with the first provider that can. Raises LLMUnavailable otherwise.

    `openai_model` and `openai_api` ("responses" or "chat") describe the OpenAI
    call this task always made, so the primary path is unchanged by the gateway.
    """
    if task not in TASKS:
        raise ValueError(f"unknown LLM task {task!r}")
    plan = _plan(task, openai_model)
    if not plan:
        raise LLMUnavailable(f"no LLM provider is configured for {task}")

    mode = "json" if json_mode else "text"
    per_call = float(timeout or os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
    budget = float(deadline or os.environ.get("LLM_DEADLINE_SECONDS", "120"))
    started = time.monotonic()
    failures: list[dict] = []

    for index, attempt in enumerate(plan):
        now = time.time()
        cooling = _cooling_until(attempt, mode, now)
        if cooling:
            failures.append({"provider": attempt.provider, "model": attempt.model,
                             "kind": "cooling_down", "reason": cooling[1],
                             "seconds_left": int(cooling[0] - now)})
            continue
        remaining = budget - (time.monotonic() - started)
        if remaining <= 1:
            failures.append({"provider": attempt.provider, "model": attempt.model,
                             "kind": "deadline"})
            break
        try:
            text, used, usage = _call(
                attempt, system=system, user=user, json_mode=json_mode, max_tokens=max_tokens,
                temperature=temperature, openai_api=openai_api, timeout=min(per_call, remaining),
            )
        except Exception as exc:  # noqa: BLE001 - classified, recorded, then the next attempt
            kind = classify(exc, attempt.provider)
            _record_failure(attempt, mode, kind, exc, time.time())
            failures.append({"provider": attempt.provider, "model": attempt.model, "kind": kind})
            continue
        if not text.strip():
            _record_failure(attempt, mode, "empty", None, time.time())
            failures.append({"provider": attempt.provider, "model": attempt.model, "kind": "empty"})
            continue
        _record_success(attempt, time.time())
        if failures:
            log.info("llm: %s answered by %s %s after %d failed or skipped attempt(s)",
                     task, attempt.provider, used, len(failures))
        return Completion(
            text=text, provider=attempt.provider, model=used, requested_model=attempt.model,
            fallback=index > 0, usage=usage, failures=failures,
        )

    raise LLMUnavailable(
        f"every LLM provider failed or is cooling down for {task}", failures=failures
    )


def status() -> dict:
    """Configuration and health, for /readyz and the status endpoint. No keys."""
    now = time.time()
    with _LOCK:
        cooling = [
            {"provider": p, "model": m, "mode": mode, "kind": kind,
             "seconds_left": int(until - now)}
            for (p, m, mode), (until, kind) in _COOLING.items() if until > now
        ]
        stats = [
            {"provider": p, "model": m, **{k: v for k, v in s.items()}}
            for (p, m), s in _STATS.items()
        ]
    return {
        "openai": {"configured": bool(os.environ.get("OPENAI_API_KEY"))},
        "openrouter": {
            "configured": bool(os.environ.get("OPENROUTER_API_KEY")),
            "models": openrouter_models(),
            "tasks": sorted(openrouter_tasks()),
            "data_collection": os.environ.get("OPENROUTER_DATA_COLLECTION", "allow") or "allow",
        },
        "primary": _primary(),
        "tasks": {t: available(t) for t in TASKS},
        "cooling_down": cooling,
        "models": stats,
    }


def summary() -> str:
    """One line for /readyz, e.g. "openai+openrouter(3)"."""
    parts = []
    if os.environ.get("OPENAI_API_KEY"):
        parts.append("openai")
    if os.environ.get("OPENROUTER_API_KEY"):
        parts.append(f"openrouter({len(openrouter_models())})")
    return "+".join(parts) or "none"
