"""Bounded, non-blocking in-memory buffers and retry backoff.

`put` never blocks and never raises: when the buffer is full it drops the oldest record and counts
it (docs/06 failure behaviour). Every counter is readable through `acp_sdk._api._stats()`.
"""

from __future__ import annotations

import random
import threading
from collections import deque
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


def backoff_delay(
    attempt: int,
    *,
    base_s: float,
    cap_s: float,
    rng: random.Random,
    retry_after_s: float | None = None,
    retry_after_cap_s: float = 60.0,
) -> float:
    """Exponential backoff with full jitter: uniform(0, min(cap, base * 2**attempt)).

    A server `Retry-After` is a floor (capped), so a throttled client never comes back early.
    """
    ceiling = min(cap_s, base_s * (2 ** max(attempt, 0)))
    delay = rng.uniform(0.0, ceiling)
    if retry_after_s is not None and retry_after_s > 0:
        delay = max(delay, min(retry_after_s, retry_after_cap_s))
    return delay


def parse_retry_after(value: str | None) -> float | None:
    """Delta-seconds only; an HTTP-date falls back to jittered backoff."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class BoundedBuffer(Generic[T]):
    """Thread-safe FIFO bounded by record count and an estimated byte size; drops oldest."""

    def __init__(
        self,
        *,
        max_items: int,
        max_bytes: int | None = None,
        size_of: Callable[[T], int] | None = None,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self._items: deque[tuple[T, int]] = deque()
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._size_of = size_of
        self._bytes = 0
        self._cond = threading.Condition()
        self.dropped = 0

    def put(self, item: T) -> int:
        """Append `item`; return how many older records were dropped to make room."""
        size = self._size_of(item) if self._size_of is not None else 0
        dropped = 0
        with self._cond:
            if self._max_bytes is not None and size > self._max_bytes:
                # A single record larger than the whole budget is dropped itself.
                self.dropped += 1
                return 1
            self._items.append((item, size))
            self._bytes += size
            while len(self._items) > self._max_items or (
                self._max_bytes is not None and self._bytes > self._max_bytes
            ):
                _, old_size = self._items.popleft()
                self._bytes -= old_size
                dropped += 1
            self.dropped += dropped
            self._cond.notify_all()
        return dropped

    def take(self, limit: int) -> list[T]:
        """Remove and return up to `limit` of the oldest records (possibly none)."""
        with self._cond:
            out: list[T] = []
            while self._items and len(out) < limit:
                item, size = self._items.popleft()
                self._bytes -= size
                out.append(item)
            return out

    def wait_for_items(self, timeout_s: float, stop: threading.Event | None = None) -> bool:
        """Wait until a record is buffered (True) or `stop` is set / the timeout elapses; `wake`
        re-checks `stop`."""
        with self._cond:
            self._cond.wait_for(
                lambda: bool(self._items) or (stop is not None and stop.is_set()),
                timeout=timeout_s,
            )
            return bool(self._items)

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)

    @property
    def size_bytes(self) -> int:
        with self._cond:
            return self._bytes
