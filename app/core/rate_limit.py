"""In-memory sliding-window rate limiter (per API key id).

Production note: single-process memory limiter. For multi-worker / multi-host
deployments, replace with Redis (same interface: check_and_hit).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check_and_hit(self, key_id: int, rpm: int) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        window = 60.0
        with self._lock:
            dq = self._hits[key_id]
            while dq and now - dq[0] > window:
                dq.popleft()
            if len(dq) >= max(1, rpm):
                retry_after = int(window - (now - dq[0])) + 1 if dq else 1
                return False, max(1, retry_after)
            dq.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
