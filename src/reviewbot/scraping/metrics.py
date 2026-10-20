"""What actually happened during a collection, per engine.

Phase 10's rule is "do not assert performance gains without measurements", and
the only way to keep that honest is for the thing being measured to count itself.
`RunMetrics` is a plain in-memory counter that each engine updates as it works.
At the end of a collection it is written to `marts.scrape_runs.by_engine`, which
is what the collection-health panel renders and what the benchmark script diffs.

The counters are chosen so that a reader can answer the questions that matter:

* did it work?            `attempted`, `succeeded`, `failed`, `success_rate`
* was it worth it?        `items`, `valid`, `duplicates`, `valid_per_request`
* what did it cost?       `duration_ms`, `bytes`, `ai_calls`, `estimated_cost_usd`
* why did it stop?        `skipped` with reasons, `stop_reason`
* where did it struggle?  `by_domain`

Nothing here is required for the pipeline to function: a metrics failure must
never fail a collection, so every public method swallows its own errors.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class EngineMetrics:
    """One engine's tally within a run."""

    engine: str
    attempted: int = 0          # fetch/extract operations started
    succeeded: int = 0          # operations that returned usable content
    failed: int = 0             # operations that errored
    items: int = 0              # candidate records produced
    valid: int = 0              # records that passed validation
    rejected: int = 0           # records dropped by validation
    duplicates: int = 0         # records dropped as already seen
    retries: int = 0
    bytes_fetched: int = 0
    duration_ms: int = 0
    ai_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    #: reason -> count, e.g. {"robots disallowed": 3, "url policy": 1}
    skipped: dict[str, int] = field(default_factory=dict)
    #: domain -> {attempted, valid, ms}
    by_domain: dict[str, dict] = field(default_factory=dict)

    @property
    def success_rate(self) -> float | None:
        return round(self.succeeded / self.attempted, 4) if self.attempted else None

    @property
    def valid_per_request(self) -> float | None:
        return round(self.valid / self.attempted, 3) if self.attempted else None

    def as_dict(self) -> dict:
        return {
            "engine": self.engine,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "items": self.items,
            "valid": self.valid,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "retries": self.retries,
            "bytes_fetched": self.bytes_fetched,
            "duration_ms": self.duration_ms,
            "success_rate": self.success_rate,
            "valid_per_request": self.valid_per_request,
            "ai_calls": self.ai_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "skipped": dict(self.skipped),
            "by_domain": {d: dict(v) for d, v in self.by_domain.items()},
        }


class RunMetrics:
    """Thread-safe per-engine counters for one collection run."""

    def __init__(self) -> None:
        self._engines: dict[str, EngineMetrics] = {}
        # RLock, not Lock: `engine()` takes the lock and several recorders want to
        # call it while holding it. With a plain Lock that is a silent deadlock
        # (no exception, the collection thread just stops), which is exactly the
        # failure a metrics helper must not be able to cause.
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self.stop_reason = ""

    # -- recording ----------------------------------------------------------

    def engine(self, name: str) -> EngineMetrics:
        with self._lock:
            m = self._engines.get(name)
            if m is None:
                m = EngineMetrics(engine=name)
                self._engines[name] = m
            return m

    def attempt(self, engine: str, domain: str = "") -> None:
        try:
            m = self.engine(engine)
            with self._lock:
                m.attempted += 1
                if domain:
                    d = m.by_domain.setdefault(domain, {"attempted": 0, "valid": 0, "ms": 0})
                    d["attempted"] += 1
        except Exception:  # noqa: BLE001
            pass

    def success(
        self, engine: str, *, items: int = 0, duration_ms: int = 0,
        nbytes: int = 0, domain: str = "",
    ) -> None:
        try:
            m = self.engine(engine)
            with self._lock:
                m.succeeded += 1
                m.items += items
                m.duration_ms += max(0, duration_ms)
                m.bytes_fetched += max(0, nbytes)
                if domain:
                    d = m.by_domain.setdefault(domain, {"attempted": 0, "valid": 0, "ms": 0})
                    d["ms"] += max(0, duration_ms)
        except Exception:  # noqa: BLE001
            pass

    def failure(self, engine: str, *, duration_ms: int = 0, domain: str = "") -> None:
        try:
            m = self.engine(engine)
            with self._lock:
                m.failed += 1
                m.duration_ms += max(0, duration_ms)
                if domain:
                    m.by_domain.setdefault(domain, {"attempted": 0, "valid": 0, "ms": 0})
        except Exception:  # noqa: BLE001
            pass

    def retry(self, engine: str) -> None:
        try:
            m = self.engine(engine)
            with self._lock:
                m.retries += 1
        except Exception:  # noqa: BLE001
            pass

    def skip(self, engine: str, reason: str) -> None:
        try:
            m = self.engine(engine)
            with self._lock:
                m.skipped[reason] = m.skipped.get(reason, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    def records(
        self, engine: str, *, valid: int = 0, rejected: int = 0,
        duplicates: int = 0, domain: str = "",
    ) -> None:
        try:
            m = self.engine(engine)
            with self._lock:
                m.valid += valid
                m.rejected += rejected
                m.duplicates += duplicates
                if domain:
                    d = m.by_domain.setdefault(domain, {"attempted": 0, "valid": 0, "ms": 0})
                    d["valid"] += valid
        except Exception:  # noqa: BLE001
            pass

    def ai_usage(
        self, engine: str, *, calls: int = 1, prompt_tokens: int = 0,
        completion_tokens: int = 0, cost_usd: float = 0.0,
    ) -> None:
        try:
            m = self.engine(engine)
            with self._lock:
                m.ai_calls += calls
                m.prompt_tokens += prompt_tokens
                m.completion_tokens += completion_tokens
                m.estimated_cost_usd += cost_usd
        except Exception:  # noqa: BLE001
            pass

    # -- reading ------------------------------------------------------------

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def engines_used(self) -> list[str]:
        with self._lock:
            return [name for name, m in self._engines.items() if m.attempted or m.items]

    def total_ai_calls(self) -> int:
        with self._lock:
            return sum(m.ai_calls for m in self._engines.values())

    def total_ai_cost(self) -> float:
        with self._lock:
            return round(sum(m.estimated_cost_usd for m in self._engines.values()), 6)

    def as_dict(self) -> dict:
        with self._lock:
            engines = {name: m.as_dict() for name, m in self._engines.items()}
        return {
            "elapsed_ms": self.elapsed_ms,
            "stop_reason": self.stop_reason,
            "engines": engines,
        }

    def summary_line(self) -> str:
        """One log line a human can read without opening the JSON."""
        with self._lock:
            parts = [
                f"{m.engine}={m.valid}/{m.items} ok={m.succeeded}/{m.attempted} {m.duration_ms}ms"
                + (f" ${m.estimated_cost_usd:.4f}" if m.ai_calls else "")
                for m in self._engines.values()
                if m.attempted or m.items
            ]
        return " | ".join(parts) or "no engine activity"


@dataclass
class Timer:
    """`with Timer() as t: ...` then read `t.ms`."""

    ms: int = 0
    _start: float = field(default=0.0, init=False)

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc) -> bool:
        self.ms = int((time.monotonic() - self._start) * 1000)
        return False
