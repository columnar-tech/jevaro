"""Adapt how many upstream calls are in flight to TypeSafe's rate limits.

Every upstream attempt, including the SDK's retries, passes through a
ThrottledTransport. It keeps one Throttle per API key, shared by every
incoming request and connection that uses the key.

A 429 or 529 asks the caller to slow down, not to give up, so the transport
sends a throttled call again itself for as long as calls with the same key
still succeed. A large job gets hundreds of these responses, and a count of
retries per call would end its stream after one unlucky call.
"""

import asyncio
import hashlib
import math
import random
import time

import httpx2

THROTTLED = {429, 529}
RETRY_COUNT_HEADER = "x-typesafe-retry-count"
# How long a throttled call keeps trying while no call with its key succeeds.
PATIENCE = 30.0


class Throttle:
    """An additive-increase, multiplicative-decrease limit on calls in flight.

    Each success raises the limit by about one call per round trip. A 429 or
    529 lowers it to `decrease` times the calls then in flight, at most once per
    `cooldown` seconds, so one burst of rejections counts once.
    """

    def __init__(self, ceiling, decrease=0.7, cooldown=0.5, patience=PATIENCE, clock=time.monotonic):
        self.ceiling = ceiling
        self.limit = float(ceiling)
        self.decrease = decrease
        self.cooldown = cooldown
        self.patience = patience
        self.clock = clock
        self.in_flight = 0
        self.slowed = None
        self.succeeded = None
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
            self.succeeded = self.clock()
            self.limit = min(float(self.ceiling), self.limit + 1 / self.limit)

    def stalled(self, since):
        """Whether no call has succeeded for `patience` seconds, counting from `since` at the earliest."""
        progress = since if self.succeeded is None else max(since, self.succeeded)
        return self.clock() - progress >= self.patience

    async def release(self, status):
        async with self.condition:
            self.in_flight -= 1
            self.update(status)
            self.condition.notify(max(0, self.allowed() - self.in_flight))


def retry_delay(headers, resends):
    """Seconds to wait before sending a throttled call again: TypeSafe's own delay, or else backoff."""
    for name, seconds in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        try:
            delay = float(headers.get(name, "")) * seconds
        except ValueError:  # Missing, or an HTTP date.
            continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return min(5.0, 0.5 * 2 ** resends) * (1 - 0.25 * random.random())


class ThrottledTransport(httpx2.AsyncBaseTransport):
    """Wait for a slot in the request's API key's Throttle before each attempt.

    A 429 or 529 is sent again after its delay while calls with the key still
    succeed. Once none has for the throttle's patience, or the delay is longer
    than that, the response goes back to the SDK, whose retries handle it like
    any other error.
    """

    def __init__(self, inner, throttles, ceiling, sleep=asyncio.sleep, clock=time.monotonic):
        self.inner = inner
        self.throttles = throttles
        self.ceiling = ceiling
        self.sleep = sleep
        self.clock = clock

    def throttle(self, request):
        key = hashlib.sha256(request.headers.get("authorization", "").encode()).hexdigest()
        if key not in self.throttles:
            self.throttles[key] = Throttle(self.ceiling, clock=self.clock)
        return self.throttles[key]

    async def handle_async_request(self, request):
        throttle = self.throttle(request)
        started = throttle.clock()
        resends = 0
        while True:
            await throttle.acquire()
            status = None
            try:
                response = await self.inner.handle_async_request(request)
                status = response.status_code
            finally:
                await asyncio.shield(throttle.release(status))
            if status not in THROTTLED:
                return response
            delay = retry_delay(response.headers, resends)
            if delay > throttle.patience or throttle.stalled(started):
                return response
            await response.aclose()
            await self.sleep(delay)
            resends += 1
            request.headers[RETRY_COUNT_HEADER] = str(int(request.headers.get(RETRY_COUNT_HEADER, "0")) + 1)

    async def aclose(self):
        await self.inner.aclose()
