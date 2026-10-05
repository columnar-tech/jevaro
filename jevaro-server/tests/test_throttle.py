"""Unit tests for the adaptive limit on upstream calls in flight."""

import asyncio
import unittest

import httpx2

from jevaro_server.app import Spread
from jevaro_server.throttle import Throttle, ThrottledTransport


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


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
        peak = {"value": 0, "active": 0}

        async def handle(request):
            peak["active"] += 1
            peak["value"] = max(peak["value"], peak["active"])
            await asyncio.sleep(0.005)
            peak["active"] -= 1
            return httpx2.Response(next(statuses))

        async def run():
            throttles = {}
            inner = httpx2.MockTransport(handle)
            clients = [httpx2.AsyncClient(transport=ThrottledTransport(inner, throttles, ceiling=8)) for _ in range(2)]
            headers = {"Authorization": "Bearer one"}
            first = await asyncio.gather(*(clients[i % 2].get("https://upstream.test/", headers=headers) for i in range(8)))
            self.assertEqual(sum(r.status_code == 429 for r in first), 3)
            (throttle,) = throttles.values()
            self.assertLess(throttle.limit, 8)
            await clients[0].get("https://upstream.test/", headers={"Authorization": "Bearer two"})
            self.assertEqual(len(throttles), 2)
            for client in clients:
                await client.aclose()

        asyncio.run(run())
        self.assertLessEqual(peak["value"], 8)


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
