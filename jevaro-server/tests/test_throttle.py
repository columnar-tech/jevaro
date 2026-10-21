"""Unit tests for the adaptive limit on upstream calls in flight."""

import asyncio
import unittest

import httpx2

from jevaro_server.app import Spread
from jevaro_server.throttle import Throttle, ThrottledTransport, retry_delay


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


async def no_sleep(seconds):
    pass


class ThrottleTests(unittest.TestCase):
    def test_successes_raise_the_limit_up_to_the_ceiling(self):
        throttle = Throttle(10)
        throttle.limit = 4.0
        for _ in range(4):
            throttle.update(200)
        self.assertGreater(throttle.limit, 4.9)
        for _ in range(1000):
            throttle.update(200)
        self.assertEqual(throttle.limit, 10.0)

    def test_rejections_lower_the_limit_once_per_cooldown(self):
        clock = Clock()
        throttle = Throttle(100, clock=clock)
        throttle.in_flight = 39
        throttle.update(429)
        self.assertAlmostEqual(throttle.limit, 28.0)
        throttle.update(529)
        self.assertAlmostEqual(throttle.limit, 28.0)
        clock.now = 0.6
        throttle.in_flight = 9
        throttle.update(429)
        self.assertAlmostEqual(throttle.limit, 7.0)
        for _ in range(10):
            clock.now += 1
            throttle.update(429)
        self.assertEqual(throttle.limit, 1.0)
        self.assertEqual(throttle.allowed(), 1)

    def test_other_failures_leave_the_limit_alone(self):
        throttle = Throttle(10)
        throttle.limit = 5.0
        for status in (None, 401, 422, 500):
            throttle.update(status)
        self.assertEqual(throttle.limit, 5.0)

    def test_stalled_once_nothing_succeeds_for_its_patience(self):
        clock = Clock()
        throttle = Throttle(10, patience=30, clock=clock)
        clock.now = 29.9
        self.assertFalse(throttle.stalled(since=0))
        clock.now = 30
        self.assertTrue(throttle.stalled(since=0))
        # A success restarts the wait for every call; a later call gets its own.
        clock.now = 25
        throttle.update(200)
        clock.now = 50
        self.assertFalse(throttle.stalled(since=0))
        clock.now = 55
        self.assertTrue(throttle.stalled(since=0))
        self.assertFalse(throttle.stalled(since=40))
        throttle.update(429)
        self.assertTrue(throttle.stalled(since=0))

    def test_retry_delay_follows_typesafe_then_backs_off(self):
        self.assertEqual(retry_delay(httpx2.Headers({"retry-after-ms": "886", "retry-after": "1"}), 0), 0.886)
        self.assertEqual(retry_delay(httpx2.Headers({"retry-after": "2"}), 0), 2.0)
        for headers in ({}, {"retry-after": "Wed, 21 Oct 2026 14:15:55 GMT"}, {"retry-after-ms": "-5"}):
            self.assertTrue(0.375 <= retry_delay(httpx2.Headers(headers), 0) <= 0.5)
        self.assertTrue(3.75 <= retry_delay(httpx2.Headers(), 10) <= 5.0)

    def test_acquire_waits_for_a_slot(self):
        async def run():
            throttle = Throttle(2)
            await throttle.acquire()
            await throttle.acquire()
            waiting = asyncio.create_task(throttle.acquire())
            await asyncio.sleep(0.01)
            self.assertFalse(waiting.done())
            await throttle.release(200)
            await asyncio.wait_for(waiting, 1)
            self.assertEqual(throttle.in_flight, 2)

        asyncio.run(run())


class TransportTests(unittest.TestCase):
    def test_rejections_slow_every_connection_for_the_same_key(self):
        statuses = iter([429] * 3 + [200] * 100)
        peak = {"value": 0, "active": 0, "calls": 0}

        async def handle(request):
            peak["active"] += 1
            peak["calls"] += 1
            peak["value"] = max(peak["value"], peak["active"])
            await asyncio.sleep(0.005)
            peak["active"] -= 1
            return httpx2.Response(next(statuses))

        async def run():
            throttles = {}
            inner = httpx2.MockTransport(handle)
            clients = [httpx2.AsyncClient(transport=ThrottledTransport(inner, throttles, ceiling=8, sleep=no_sleep))
                       for _ in range(2)]
            headers = {"Authorization": "Bearer one"}
            first = await asyncio.gather(*(clients[i % 2].get("https://upstream.test/", headers=headers) for i in range(8)))
            self.assertEqual([r.status_code for r in first], [200] * 8)
            self.assertEqual(peak["calls"], 11)
            (throttle,) = throttles.values()
            self.assertLess(throttle.limit, 8)
            await clients[0].get("https://upstream.test/", headers={"Authorization": "Bearer two"})
            self.assertEqual(len(throttles), 2)
            for client in clients:
                await client.aclose()

        asyncio.run(run())
        self.assertLessEqual(peak["value"], 8)

    def test_throttled_calls_go_again_while_others_succeed(self):
        clock = Clock()
        stuck = []

        async def handle(request):
            if request.url.path == "/fine":
                return httpx2.Response(200)
            stuck.append(request.headers.get("x-typesafe-retry-count"))
            return httpx2.Response(429 if len(stuck) <= 40 else 200, headers={"retry-after-ms": "1000"})

        async def sleep(seconds):
            await clock.sleep(seconds)
            if clock.now % 10 == 0:  # Another call with the key succeeds every 10 seconds.
                await client.get("https://upstream.test/fine")

        async def run():
            nonlocal client
            transport = ThrottledTransport(httpx2.MockTransport(handle), {}, ceiling=8, sleep=sleep, clock=clock)
            async with httpx2.AsyncClient(transport=transport) as client:
                # The SDK's own retry count goes up with each attempt.
                return await client.get("https://upstream.test/stuck", headers={"x-typesafe-retry-count": "2"})

        client = None
        self.assertEqual(asyncio.run(run()).status_code, 200)
        self.assertEqual(stuck, [str(n) for n in range(2, 43)])
        self.assertEqual(clock.now, 40.0)

    def test_a_stalled_key_hands_the_rejection_back(self):
        clock = Clock()
        attempts = []

        async def handle(request):
            attempts.append(clock.now)
            return httpx2.Response(429, headers={"retry-after-ms": "1000"})

        async def run():
            transport = ThrottledTransport(httpx2.MockTransport(handle), {}, ceiling=8, sleep=clock.sleep, clock=clock)
            async with httpx2.AsyncClient(transport=transport) as client:
                return await client.get("https://upstream.test/")

        self.assertEqual(asyncio.run(run()).status_code, 429)
        self.assertEqual(attempts, [float(t) for t in range(31)])

    def test_a_long_delay_hands_the_rejection_back_at_once(self):
        attempts = []

        async def handle(request):
            attempts.append(request)
            return httpx2.Response(429, headers={"retry-after": "120"})

        async def run():
            async with httpx2.AsyncClient(transport=ThrottledTransport(
                    httpx2.MockTransport(handle), {}, ceiling=8, sleep=no_sleep)) as client:
                return await client.get("https://upstream.test/")

        self.assertEqual(asyncio.run(run()).status_code, 429)
        self.assertEqual(len(attempts), 1)

    def test_other_failures_go_back_at_once(self):
        attempts = []

        async def handle(request):
            attempts.append(request)
            return httpx2.Response(503, headers={"retry-after-ms": "1"})

        async def run():
            async with httpx2.AsyncClient(transport=ThrottledTransport(
                    httpx2.MockTransport(handle), {}, ceiling=8, sleep=no_sleep)) as client:
                return await client.get("https://upstream.test/")

        self.assertEqual(asyncio.run(run()).status_code, 503)
        self.assertEqual(len(attempts), 1)


class SpreadTests(unittest.TestCase):
    def test_each_call_goes_to_the_least_busy_connection(self):
        class Client:
            def __init__(self):
                self.calls = 0

            async def system_one(self, **kwargs):
                self.calls += 1
                await asyncio.sleep(0.01 * kwargs["state"])
                return kwargs["state"]

        async def run():
            clients = [Client(), Client()]
            spread = Spread(clients, [0, 0])
            slow = asyncio.create_task(spread.system_one(state=5))
            await asyncio.sleep(0)
            results = [await spread.system_one(state=0) for _ in range(3)]
            self.assertEqual(await slow, 5)
            return clients, results

        clients, results = asyncio.run(run())
        self.assertEqual(results, [0, 0, 0])
        self.assertEqual([client.calls for client in clients], [1, 3])


if __name__ == "__main__":
    unittest.main()
