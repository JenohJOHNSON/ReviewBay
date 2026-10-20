"""Budgets: the limits that keep a collection bounded in time, requests and money.

A collection that can discover its own URLs can, in principle, crawl forever.
Three different things need bounding and they are genuinely different:

* **Politeness** — how fast we may hit *one* domain (`RateLimiter`). Driven by
  robots.txt `Crawl-delay`, defaulting to one request per second per domain.
* **Effort** — how much work a *run* may do in total: pages, requests per
  domain, wall-clock, concurrent fetches (`CrawlBudget`).
* **Money** — how many AI-extraction calls a run may make and what they may cost
  (`SpendBudget`). This is the one that can produce a surprising bill, so it is
  enforced *before* each call, counted in tokens, and reported in the run record.

Everything is per-run and in-process. Nothing here persists, so a restart loses
the counters — which is correct for "per run" semantics and means no shared state
to corrupt.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from . import robots, urlpolicy

log = logging.getLogger(__name__)


# --- politeness --------------------------------------------------------------

class RateLimiter:
    """One request per domain per `delay` seconds, shared across threads.

    Sleeps the calling thread. That is deliberate: these fetches already run on a
    worker thread off the FastAPI event loop, and a sleeping worker is exactly
    the back-pressure we want.
    """

    def __init__(self, default_delay: float | None = None) -> None:
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()
        self._default = default_delay if default_delay is not None else robots.DEFAULT_DELAY

    def delay_for(self, url: str) -> float:
        try:
            return robots.crawl_delay(url)
        except Exception:  # noqa: BLE001 — never let politeness break a fetch
            return self._default

    def wait(self, url: str) -> float:
        """Block until this domain may be hit again. Returns seconds waited."""
        domain = urlpolicy.registrable_domain(url) or url
        delay = self.delay_for(url)
        while True:
            now = time.monotonic()
            with self._lock:
                ready_at = self._next_allowed.get(domain, 0.0)
                if now >= ready_at:
                    self._next_allowed[domain] = now + delay
                    return 0.0
                sleep_for = ready_at - now
                # Reserve our slot so concurrent callers queue instead of racing.
                self._next_allowed[domain] = ready_at + delay
            time.sleep(sleep_for)
            return sleep_for


# --- effort ------------------------------------------------------------------

@dataclass
class CrawlBudget:
    """How much fetching one collection run may do.

    `max_pages` is the hard stop on total fetches. `max_pages_per_domain` stops
    one chatty site from consuming the whole budget. `deadline_seconds` bounds
    wall-clock so a collection cannot outlive a user's patience (or a platform
    request timeout) however many sources are healthy.
    """

    max_pages: int = int(os.environ.get("SCRAPLING_MAX_PAGES", "40"))
    max_pages_per_domain: int = int(os.environ.get("SCRAPLING_MAX_PAGES_PER_DOMAIN", "8"))
    max_concurrency: int = int(os.environ.get("SCRAPLING_MAX_CONCURRENCY", "4"))
    request_timeout: float = float(os.environ.get("SCRAPLING_REQUEST_TIMEOUT", "20"))
    deadline_seconds: float = float(os.environ.get("SCRAPING_DEADLINE_SECONDS", "240"))
    max_bytes_per_page: int = int(os.environ.get("SCRAPING_MAX_PAGE_BYTES", str(3 * 1024 * 1024)))

    _started: float = field(default_factory=time.monotonic, init=False)
    _pages: int = field(default=0, init=False)
    _per_domain: dict[str, int] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    #: Why the budget stopped the run, for the run record.
    stop_reason: str = field(default="", init=False)

    @property
    def pages_fetched(self) -> int:
        return self._pages

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining_pages(self) -> int:
        return max(0, self.max_pages - self._pages)

    def expired(self) -> bool:
        if self.deadline_seconds <= 0:
            return False
        if self.elapsed >= self.deadline_seconds:
            self.stop_reason = self.stop_reason or "deadline reached"
            return True
        return False

    def allows(self, url: str) -> bool:
        """Whether one more fetch of `url` fits. Does NOT consume the slot."""
        if self.expired():
            return False
        with self._lock:
            if self._pages >= self.max_pages:
                self.stop_reason = self.stop_reason or "page budget exhausted"
                return False
            domain = urlpolicy.registrable_domain(url) or url
            return self._per_domain.get(domain, 0) < self.max_pages_per_domain

    def take(self, url: str) -> bool:
        """Claim one fetch slot for `url`. False when the budget is spent."""
        if self.expired():
            return False
        domain = urlpolicy.registrable_domain(url) or url
        with self._lock:
            if self._pages >= self.max_pages:
                self.stop_reason = self.stop_reason or "page budget exhausted"
                return False
            used = self._per_domain.get(domain, 0)
            if used >= self.max_pages_per_domain:
                return False
            self._pages += 1
            self._per_domain[domain] = used + 1
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "pages_fetched": self._pages,
                "max_pages": self.max_pages,
                "domains": dict(self._per_domain),
                "elapsed_ms": int(self.elapsed * 1000),
                "stop_reason": self.stop_reason,
            }


# --- money -------------------------------------------------------------------

# Approximate USD per 1M tokens, used only to enforce a ceiling and to report an
# estimate. These are deliberately coarse: the number shown to an operator is
# labelled an estimate everywhere it appears, and the ceiling is the real control.
# Override with SCRAPEGRAPH_PRICE_IN / SCRAPEGRAPH_PRICE_OUT when a model's price
# changes or a model is missing here.
_PRICES_PER_MTOK = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
}
_DEFAULT_PRICE = (0.50, 2.00)  # unknown model: assume mid-range, never free


def model_price(model: str) -> tuple[float, float]:
    """(input, output) USD per million tokens for `model`."""
    key = (model or "").strip().lower()
    # OpenRouter's free models, and its router over them, cost nothing. Checked
    # before the override so a price set for the paid model is not charged for
    # calls a free fallback answered.
    if key.endswith(":free") or key == "openrouter/free":
        return 0.0, 0.0
    override_in = os.environ.get("SCRAPEGRAPH_PRICE_IN")
    override_out = os.environ.get("SCRAPEGRAPH_PRICE_OUT")
    if override_in and override_out:
        try:
            return float(override_in), float(override_out)
        except ValueError:
            log.warning("ignoring non-numeric SCRAPEGRAPH_PRICE_IN/OUT")
    for name, price in _PRICES_PER_MTOK.items():
        if key == name or key.endswith("/" + name):
            return price
    return _DEFAULT_PRICE


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimated USD for one call. Labelled an estimate wherever it is shown."""
    price_in, price_out = model_price(model)
    return (prompt_tokens / 1_000_000) * price_in + (completion_tokens / 1_000_000) * price_out


def estimate_tokens(text: str) -> int:
    """Token count without a tokenizer dependency.

    ~4 characters per token for English prose is the standard rough figure, and
    it only needs to be good enough to refuse an oversized page before paying for
    it. When `tiktoken` is present (it arrives with ScrapeGraphAI) the real count
    is used instead.
    """
    if not text:
        return 0
    try:
        import tiktoken  # type: ignore

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:  # noqa: BLE001 — not installed, or an encoding it lacks
        return max(1, len(text) // 4)


class BudgetExceeded(RuntimeError):
    """Raised when an AI extraction would breach the run's limits."""


@dataclass
class SpendBudget:
    """The ceiling on AI-assisted extraction for one collection run.

    Checked with `reserve()` *before* a call, so an over-budget call is never
    made, then reconciled with `record()` using the provider's real token counts.

    Every limit treats 0 as "none allowed" and a negative number as "unlimited".
    """

    max_calls: int = int(os.environ.get("SCRAPEGRAPH_MAX_CALLS_PER_RUN", "10"))
    max_cost_usd: float = float(os.environ.get("SCRAPEGRAPH_MAX_COST_PER_RUN", "0.50"))
    max_input_tokens_per_call: int = int(os.environ.get("SCRAPEGRAPH_MAX_INPUT_TOKENS", "12000"))
    model: str = os.environ.get("SCRAPEGRAPH_MODEL", "gpt-4o-mini")

    calls: int = field(default=0, init=False)
    prompt_tokens: int = field(default=0, init=False)
    completion_tokens: int = field(default=0, init=False)
    cost_usd: float = field(default=0.0, init=False)
    refusals: int = field(default=0, init=False)
    last_refusal: str = field(default="", init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def would_allow(self, input_tokens: int) -> tuple[bool, str]:
        """Whether a call of this size fits, and why not when it does not.

        **Zero means zero, not unlimited.** This previously read
        `if self.max_calls > 0 and ...`, so `SCRAPEGRAPH_MAX_CALLS_PER_RUN=0` —
        the obvious way for an operator to say "make no AI calls" — disabled the
        check entirely and allowed unlimited calls. A limit that inverts at its
        safest-looking value is worse than no limit. Use a NEGATIVE value for
        unlimited, which nobody sets by accident.

        The cost ceiling is checked FIRST so an over-budget call reports the
        ceiling rather than whichever other limit happens to trip too.
        """
        with self._lock:
            if 0 <= self.max_calls <= self.calls:
                return False, f"call budget exhausted ({self.calls}/{self.max_calls})"
            if self.max_cost_usd >= 0:
                # Assume the worst for the reply: a full completion at the output
                # price. Better to refuse a borderline call than to overshoot.
                projected = self.cost_usd + estimate_cost(self.model, input_tokens, 1500)
                if projected > self.max_cost_usd:
                    return False, (
                        f"cost ceiling would be breached "
                        f"(${projected:.4f} > ${self.max_cost_usd:.2f})"
                    )
            if 0 <= self.max_input_tokens_per_call < input_tokens:
                return False, (
                    f"content too large for one call "
                    f"({input_tokens} > {self.max_input_tokens_per_call} tokens)"
                )
        return True, ""

    def reserve(self, input_tokens: int) -> None:
        """Claim one call. Raises `BudgetExceeded` when it does not fit."""
        ok, why = self.would_allow(input_tokens)
        if not ok:
            with self._lock:
                self.refusals += 1
                self.last_refusal = why
            raise BudgetExceeded(why)
        with self._lock:
            self.calls += 1

    def record(self, prompt_tokens: int, completion_tokens: int, model: str | None = None) -> None:
        """Reconcile a completed call with the provider's real token counts.

        `model` is the one that actually answered, when that differs from the
        configured one (a free fallback): the cost follows the model used.
        """
        with self._lock:
            self.prompt_tokens += max(0, int(prompt_tokens or 0))
            self.completion_tokens += max(0, int(completion_tokens or 0))
            self.cost_usd += estimate_cost(
                model or self.model,
                max(0, int(prompt_tokens or 0)),
                max(0, int(completion_tokens or 0)),
            )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "max_calls": self.max_calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "estimated_cost_usd": round(self.cost_usd, 6),
                "max_cost_usd": self.max_cost_usd,
                "refusals": self.refusals,
                "last_refusal": self.last_refusal,
                "model": self.model,
            }
