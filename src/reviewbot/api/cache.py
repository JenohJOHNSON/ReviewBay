"""A bounded, TTL-expiring, thread-safe in-process cache.

`insights.py` and `report.py` both cached their generated payloads per brand in a
plain dict with a timestamp, checking the TTL on read. That expires entries
logically but never removes them, so the dict only ever grows: one entry per
distinct brand string ever asked for, each holding a full insights or report
payload, for the life of the process.

The brand arrives as a query parameter. It is length-limited and validated, but
"valid" is not "known" — any well-formed string that matches no brand still
produces a cacheable payload and therefore a permanent entry. That is a slow
memory leak driven by request input, which is the part that makes it worth
fixing rather than tolerating.

So entries are evicted on three grounds:

* **expiry** — past the TTL, dropped on the next access rather than lingering.
* **capacity** — over `maxsize`, the least recently used entry goes. An LRU is
  the right policy here because the access pattern is a handful of brands looked
  at repeatedly, so a bounded cache keeps the entries that are actually serving
  traffic and discards the one-off lookups.
* **explicitly** — `invalidate()` for one brand, `clear()` for all, which is
  what the `refresh=True` request parameter needs.

`maxsize` defaults to 64 brands rather than something larger because each entry
is a whole report payload and the dashboard's own brand list is per-browser and
small; a deployment tracking more can raise it. The cache is per process, so a
multi-worker deployment holds one per worker — fine for a read-through cache of
derived data, and the reason this is not where anything authoritative lives.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

DEFAULT_MAXSIZE = 64


class TTLCache:
    """Bounded LRU cache whose entries also expire after `ttl` seconds."""

    def __init__(self, ttl: float, maxsize: int = DEFAULT_MAXSIZE, name: str = "cache"):
        if ttl < 0:
            raise ValueError("ttl must not be negative")
        self.ttl = float(ttl)
        # A maxsize below 1 would make put() a no-op and silently disable
        # caching, so the floor is 1 rather than 0.
        self.maxsize = max(1, int(maxsize))
        self.name = name
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: str) -> Any | None:
        """Return the cached value, or None if absent or expired.

        A stored value of None is indistinguishable from a miss. Both callers
        cache dicts, and treating a None as a miss simply regenerates it, so
        this stays a plain Optional rather than carrying a sentinel.
        """
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, value = entry
            if now - stored_at >= self.ttl:
                # Expired: remove it now rather than leaving it to be stepped
                # over on every future lookup.
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: Any) -> None:
        now = time.time()
        with self._lock:
            self._entries[key] = (now, value)
            self._entries.move_to_end(key)
            self._evict_expired(now)
            while len(self._entries) > self.maxsize:
                # popitem(last=False) is the least recently used end.
                self._entries.popitem(last=False)
                self.evictions += 1

    def invalidate(self, key: str) -> bool:
        """Drop one entry. Returns whether there was one."""
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, Any]:
        """Counters for the collection-health panel and for tests."""
        with self._lock:
            return {
                "name": self.name,
                "entries": len(self._entries),
                "maxsize": self.maxsize,
                "ttl": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def _evict_expired(self, now: float) -> None:
        """Caller must hold the lock."""
        dead = [k for k, (stored_at, _v) in self._entries.items() if now - stored_at >= self.ttl]
        for k in dead:
            del self._entries[k]
