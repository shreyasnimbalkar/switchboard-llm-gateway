"""Exact-match response cache (in memory, LRU + TTL).

If a client sends the exact same request twice, the second one is answered instantly
and for free. Step 4 adds a semantic cache that also matches *similar* questions.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Callable


def cache_key(payload: dict[str, Any]) -> str:
    """Stable hash of everything that affects the answer (ignores `stream`)."""
    relevant = {k: v for k, v in payload.items() if k not in ("stream", "stream_options")}
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ExactCache:
    def __init__(self, ttl_s: float, max_entries: int, clock: Callable[[], float] = time.monotonic):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.clock = clock
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self.clock() >= expires_at:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        if self.max_entries <= 0:
            return
        self._items[key] = (self.clock() + self.ttl_s, value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)
