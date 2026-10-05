"""Adapt how many upstream calls are in flight to TypeSafe's rate limits.

Every upstream attempt, including the SDK's retries, passes through a
ThrottledTransport. It keeps one Throttle per API key, shared by every
incoming request and connection that uses the key.
"""

import asyncio
import hashlib
import time

import httpx2

THROTTLED = {429, 529}


class Throttle:
    """An additive-increase, multiplicative-decrease limit on calls in flight.

    Each success raises the limit by about one call per round trip. A 429 or
    529 lowers it to `decrease` times the calls then in flight, at most once per
    `cooldown` seconds, so one burst of rejections counts once.
    """

    def __init__(self, ceiling, decrease=0.7, cooldown=0.5, clock=time.monotonic):
        self.ceiling = ceiling
        self.limit = float(ceiling)
        self.decrease = decrease
        self.cooldown = cooldown
        self.clock = clock
        self.in_flight = 0
        self.slowed = None
        self.condition = None

    def allowed(self):
        return max(1, int(self.limit))

    async def acquire(self):
        if self.condition is None:
            self.condition = asyncio.Condition()
        async with self.condition:
            await self.condition.wait_for(lambda: self.in_flight < self.allowed())
            self.in_flight += 1

    def update(self, status):
        """Adjust the limit for one finished attempt; status is None if it failed to connect."""
        if status in THROTTLED:
            now = self.clock()
            if self.slowed is None or now - self.slowed >= self.cooldown:
                self.slowed = now
                self.limit = max(1.0, min(self.limit, self.in_flight + 1) * self.decrease)
        elif status is not None and status < 400:
            self.limit = min(float(self.ceiling), self.limit + 1 / self.limit)

    async def release(self, status):
        async with self.condition:
            self.in_flight -= 1
            self.update(status)
            self.condition.notify(max(0, self.allowed() - self.in_flight))


class ThrottledTransport(httpx2.AsyncBaseTransport):
    """Wait for a slot in the request's API key's Throttle before each attempt."""

    def __init__(self, inner, throttles, ceiling):
        self.inner = inner
        self.throttles = throttles
        self.ceiling = ceiling

    def throttle(self, request):
        key = hashlib.sha256(request.headers.get("authorization", "").encode()).hexdigest()
        if key not in self.throttles:
            self.throttles[key] = Throttle(self.ceiling)
        return self.throttles[key]

    async def handle_async_request(self, request):
        throttle = self.throttle(request)
        await throttle.acquire()
        status = None
        try:
            response = await self.inner.handle_async_request(request)
            status = response.status_code
            return response
        finally:
            await asyncio.shield(throttle.release(status))

    async def aclose(self):
        await self.inner.aclose()
