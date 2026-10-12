"""In-memory stores for the public endpoints' per-IP rate limits and per-email cooldowns.

Every public route used to keep its own `defaultdict(list)` keyed by client IP. Those
never dropped a key: each new visitor (or each address a scanner rotates through) added
an entry for the life of the process. These stores forget a key once nothing in it can
matter any more (its newest timestamp is outside the window), and hold at most
`max_keys` keys — beyond that the least recently seen key is dropped, which at worst
gives that one visitor a fresh window.

Per instance, not global: Cloud Run runs several instances and each counts on its own,
so a visitor spread across N instances gets up to N × the limit (deploy/RUNBOOK.md,
"Rate limits"). The limits are abuse brakes, not quotas; a shared store (Redis /
Memorystore) is the upgrade if they ever need to be exact.

Thread-safe: the route handlers are plain functions running in FastAPI's threadpool.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

DEFAULT_MAX_KEYS = 10_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RateLimitLog:
    """A sliding-window request log per key (usually a client IP)."""

    def __init__(self, window_seconds: float, max_keys: int = DEFAULT_MAX_KEYS):
        self.window = timedelta(seconds=window_seconds)
        self.max_keys = max_keys
        self._hits: OrderedDict[str, list[datetime]] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, now: Optional[datetime] = None) -> bool:
        """Record one request for `key` and return True — or return False, recording
        nothing, when `key` already made `limit` requests inside the window."""
        now = now or _now()
        cutoff = now - self.window
        with self._lock:
            recent = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(recent) >= limit:
                self._hits[key] = recent
                self._hits.move_to_end(key)
                return False
            recent.append(now)
            self._hits[key] = recent
            self._hits.move_to_end(key)
            self._evict(cutoff)
            return True

    def _evict(self, cutoff: datetime) -> None:
        # Keys are ordered by last touch, so the expired ones are at the front.
        while self._hits:
            oldest_key, times = next(iter(self._hits.items()))
            if len(self._hits) > self.max_keys or not times or times[-1] <= cutoff:
                self._hits.popitem(last=False)
            else:
                break

    # Mapping-style access (tests seed and reset the stores directly).
    def __getitem__(self, key: str) -> list[datetime]:
        with self._lock:
            return list(self._hits.get(key, ()))

    def __setitem__(self, key: str, times: list[datetime]) -> None:
        with self._lock:
            self._hits[key] = list(times)
            self._hits.move_to_end(key)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._hits

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


class RecentKeys:
    """When each key (an email, a message fingerprint) was last seen, forgotten after
    `ttl_seconds`."""

    def __init__(self, ttl_seconds: float, max_keys: int = DEFAULT_MAX_KEYS):
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_keys = max_keys
        self._seen: OrderedDict[str, datetime] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, now: Optional[datetime] = None) -> Optional[datetime]:
        """When `key` was last seen, or None if never or longer ago than the TTL."""
        now = now or _now()
        with self._lock:
            when = self._seen.get(key)
            return when if when is not None and when > now - self.ttl else None

    def __setitem__(self, key: str, when: datetime) -> None:
        with self._lock:
            self._seen[key] = when
            self._seen.move_to_end(key)
            cutoff = when - self.ttl
            while self._seen:
                oldest = next(iter(self._seen.values()))
                if len(self._seen) > self.max_keys or oldest <= cutoff:
                    self._seen.popitem(last=False)
                else:
                    break

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()
