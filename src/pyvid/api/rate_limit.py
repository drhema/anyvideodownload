"""Sliding-window rate limiter keyed by bearer-token (or 'anonymous').

In-memory and single-process. Fine for dev and low-scale deployments; swap
for Redis-backed limiter if you need multi-worker deployments.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException


class RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max = max_per_minute
        self.window = 60.0
        self._buckets: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> None:
        if self.max <= 0:
            return
        now = time.monotonic()
        q = self._buckets[key]
        cutoff = now - self.window
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.max:
            retry_in = self.window - (now - q[0])
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit: {self.max}/min. Retry in {retry_in:.1f}s.",
                headers={"Retry-After": str(max(1, int(retry_in)))},
            )
        q.append(now)
