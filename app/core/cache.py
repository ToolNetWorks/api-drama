"""Small thread-safe TTL cache for upstream metadata."""
from __future__ import annotations

import threading
import time
from typing import Any, Optional


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            value, expires = item
            if now > expires:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        with self._lock:
            self._store[key] = (value, time.time() + ttl_s)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


cache = TTLCache()
