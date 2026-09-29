"""Offline concurrency, HTTP retry, and virtual-time rate-control tests."""

import io
import json
import threading
import time
import tempfile
import unittest
from collections import Counter, deque
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx2
import pyarrow as pa
from typesafe_sdk import TypeSafeAPIError, TypeSafeClient

from arrow_results import evaluate_states
from many_states import main
from request_scheduler import AdaptiveRateLimiter, retry_after_seconds
from test_arrow_results import FakeClient, fixture


def api_error(status=429, headers=None):
    return TypeSafeAPIError(status, {"error": "test congestion"}, httpx2.Headers(headers or {}))


def fast_limiter():
    return AdaptiveRateLimiter(
        requests_per_minute=60_000, tokens_per_second=10_000_000,
        backoff_initial=0.001, backoff_max=0.002,
    )


class RateControllerTests(unittest.TestCase):
    def test_defaults_pace_starts_without_catchup_bursts(self):
        limiter = AdaptiveRateLimiter()
        self.assertEqual(limiter.target_rps, 19)
        self.assertEqual(limiter.token_budget, 237_500)
        limiter.started(100, 100)
        self.assertAlmostEqual(limiter.delay(100, 100), 1 / 19)
        self.assertEqual(limiter.delay(150, 100), 0)
        limiter.started(150, 100)
        self.assertAlmostEqual(limiter.delay(150, 100), 1 / 19)

    def test_retry_headers_numeric_milliseconds_dates_and_invalid_values(self):
        date = format_datetime(datetime.fromtimestamp(1_000, timezone.utc), usegmt=True)
        cases = [
            ({"Retry-After": "2.5"}, 2.5),
            ({"retry-after-ms": "1500", "Retry-After": "8"}, 1.5),
            ({"retry-after-ms": "broken", "Retry-After": "8"}, 8),
            ({"Retry-After": date}, 100),
            ({"Retry-After": "0"}, 0),
            ({"Retry-After": "-1"}, None),
            ({"Retry-After": "NaN"}, None),
            ({"Retry-After": "Infinity"}, None),
            ({"Retry-After": "not a date"}, None),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(retry_after_seconds(headers, wall_time=900), expected)
        self.assertEqual(retry_after_seconds({"Retry-After": date}, wall_time=1_100), 0)

    def test_one_reduction_for_inflight_group_and_unreset_quota(self):
        limiter = AdaptiveRateLimiter()
        first = limiter.started(100, 100)
        second = limiter.started(100.1, 100)
        with self.assertLogs("request_scheduler", level="WARNING"):
            limiter.failed(101, first, api_error(headers={"Retry-After": "120"}), 0)
            reduced = limiter.target_rps
            limiter.failed(101.1, second, api_error(), 0)
            self.assertEqual(limiter.target_rps, reduced)
            self.assertGreaterEqual(limiter.delay(101.1, 100), 119.9 - 1e-9)
            # A still-exhausted quota is not evidence of another lower limit.
            third = limiter.started(222, 100)
            limiter.failed(222.1, third, api_error(), 1)
            self.assertEqual(limiter.target_rps, reduced)
            good = limiter.started(230, 100)
            limiter.succeeded(230.1, good, 100)
            overloaded = limiter.started(231, 100)
            limiter.failed(231.1, overloaded, api_error(529), 0)
            self.assertLess(limiter.target_rps, reduced)

    def test_rolling_token_budget_and_usage_correction(self):
        limiter = AdaptiveRateLimiter(requests_per_minute=60_000, tokens_per_second=1_000)
        attempt = limiter.started(10, 600)
        self.assertAlmostEqual(limiter.delay(10.1, 400), 0.9)
        limiter.succeeded(10.2, attempt, 800)
        self.assertEqual(attempt.reserved_tokens, 800)
        self.assertGreater(limiter.estimate_tokens(600), 800)
        self.assertAlmostEqual(limiter.delay(10.3, 200), 0.7)
        self.assertEqual(limiter.delay(11.01, 400), 0)
        with self.assertRaisesRegex(ValueError, "token reservation"):
            limiter.delay(12, 951)

    def test_recovery_and_reprobe_stay_below_configured_ceiling(self):
        limiter = AdaptiveRateLimiter(recovery_seconds=1, probe_seconds=5)
        attempt = limiter.started(100, 1)
        with self.assertLogs("request_scheduler", level="WARNING"):
            limiter.failed(100.01, attempt, api_error(), 0)
        reduced = limiter.target_rps
        for i in range(200):
            now = 110 + i * 0.2
            ticket = limiter.started(now, 1)
            limiter.succeeded(now, ticket, 1)
            self.assertLessEqual(limiter.target_rps, 19)
        self.assertGreater(limiter.target_rps, reduced)
        self.assertAlmostEqual(limiter.target_rps, 19)

    def test_learns_lower_rolling_minute_limit(self):
        # Realistic server simulation: only 600 accepted requests in any 60s,
        # while our advertised/default ceiling is 1200 RPM. No wall-clock sleeps.
        limiter = AdaptiveRateLimiter()
        accepted = deque()
        now = 100.0
        retries = 0
        with patch("request_scheduler.random.random", return_value=0), \
             patch("request_scheduler.LOGGER"):
            while now < 1000:
                now += limiter.delay(now, limiter.estimate_tokens(1)) + 1e-7
                attempt = limiter.started(now, 1)
                while accepted and accepted[0] <= now - 60:
                    accepted.popleft()
                if len(accepted) >= 600:
                    limiter.failed(now, attempt, api_error(), retries)
                    retries += 1
                else:
                    accepted.append(now)
                    limiter.succeeded(now, attempt, 1)
                    retries = 0
        self.assertGreater(limiter.failures[429], 0)
        self.assertGreater(limiter.target_rps, 8.5)
        self.assertLess(limiter.target_rps, 10)
        self.assertGreater(limiter.successes, 6_000)

    def test_invalid_configuration_is_rejected_without_calls(self):
        for value in (0, -1, True, float("nan"), float("inf")):
            for name in ("requests_per_minute", "tokens_per_second", "headroom"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    AdaptiveRateLimiter(**{name: value})
        client = FakeClient()
        for name, values in (("concurrency", (0, -1, True, 1.5)), ("max_retries", (-1, True, 1.5))):
            for value in values:
                with self.assertRaises(ValueError):
                    evaluate_states(["one"], client, **{name: value})
        self.assertEqual(client.calls, [])


class ParallelSDKTests(unittest.TestCase):
    def test_concurrent_out_of_order_http_results_keep_arrow_order_and_laziness(self):
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        finished = []
        calls = []

        def handler(request):
            index = int(json.loads(request.content)["state"])
            with lock:
                calls.append(index)
            barrier.wait(timeout=3)
            time.sleep((3 - index) * 0.015)
            response = fixture().model_dump(mode="json")
            response["answers"]["refund"]["noul"] = index / 10
            with lock:
                finished.append(index)
            return httpx2.Response(200, json=response)

        limiter = fast_limiter()
        with TypeSafeClient(api_key="offline-test", transport=httpx2.MockTransport(handler)) as client:
            with evaluate_states(["0", "1", "2", "3"], client, batch_size=3, concurrency=3, limiter=limiter) as reader:
                self.assertEqual(calls, [])
                rows = reader.read_next_batch().to_pylist()
                self.assertEqual([row["state"] for row in rows], ["0", "1", "2"])
                self.assertEqual([row["refund"]["noul"] for row in rows], [0, 0.1, 0.2])
                self.assertEqual(finished, [2, 1, 0])
            self.assertEqual(len(calls), 3)  # Closing does not send the fourth state.
        self.assertEqual(limiter.peak_in_flight, 3)

    def test_429_and_529_pause_all_dispatches_and_do_not_duplicate_rows(self):
        counts = Counter()
        calls = []
        rejected = []

        def handler(request):
            state = json.loads(request.content)["state"]
            calls.append((time.monotonic(), state))
            counts[state] += 1
            if state in ("a", "b") and counts[state] == 1:
                status = 429 if state == "a" else 529
                rejected.append(time.monotonic())
                return httpx2.Response(status, headers={"retry-after-ms": "50"}, json={"error": "slow down"})
            return httpx2.Response(200, json=fixture().model_dump(mode="json"))

        limiter = fast_limiter()
        with TypeSafeClient(api_key="offline-test", transport=httpx2.MockTransport(handler)) as client, \
             self.assertLogs("request_scheduler", level="WARNING"):
            table = evaluate_states(["a", "b", "c"], client, concurrency=3, limiter=limiter).read_all()
        self.assertEqual(table.column("state").to_pylist(), ["a", "b", "c"])
        self.assertEqual(counts, {"a": 2, "b": 2, "c": 1})
        self.assertEqual(limiter.attempts, 5)  # Proves there were no hidden SDK retries.
        self.assertEqual(limiter.failures, {429: 1, 529: 1})
        self.assertLess(limiter.target_rps, 950)
        for rejected_at in rejected:
            following = [when for when, state in calls if when > rejected_at]
            self.assertGreaterEqual(min(following) - rejected_at, 0.045)

    def test_retry_budget_and_permanent_http_errors(self):
        for status, retries, expected in [(429, 2, 3), (529, 2, 3), (503, 2, 3), (529, 0, 1), (401, 8, 1), (422, 8, 1)]:
            calls = []

            def handler(request):
                calls.append(request)
                return httpx2.Response(status, json={"error": "test error"})

            with self.subTest(status=status, retries=retries), \
                 TypeSafeClient(api_key="offline-test", transport=httpx2.MockTransport(handler)) as client, \
                 patch("request_scheduler.LOGGER"), self.assertRaises(TypeSafeAPIError) as error:
                evaluate_states(["a"], client, max_retries=retries, limiter=fast_limiter()).read_all()
            self.assertEqual(error.exception.status, status)
            self.assertEqual(len(calls), expected)

    def test_connection_retry_uses_same_controller(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx2.ConnectError("offline connection failure", request=request)
            return httpx2.Response(200, json=fixture().model_dump(mode="json"))

        limiter = fast_limiter()
        with TypeSafeClient(api_key="offline-test", transport=httpx2.MockTransport(handler)) as client, \
             self.assertLogs("request_scheduler", level="WARNING"):
            self.assertEqual(evaluate_states(["a"], client, limiter=limiter).read_all().num_rows, 1)
        self.assertEqual(limiter.attempts, 2)
        self.assertEqual(limiter.target_rps, 950)

    def test_permanent_failure_stops_dispatch_and_drains_inflight_calls(self):
        second_started = threading.Event()
        calls = []
        finished = []

        def handler(request):
            state = json.loads(request.content)["state"]
            calls.append(state)
            if state == "a":
                self.assertTrue(second_started.wait(timeout=3))
                return httpx2.Response(401, json={"error": "test authentication failure"})
            second_started.set()
            time.sleep(0.025)
            finished.append(state)
            return httpx2.Response(200, json=fixture().model_dump(mode="json"))

        with TypeSafeClient(api_key="offline-test", transport=httpx2.MockTransport(handler)) as client:
            with self.assertRaises(TypeSafeAPIError):
                evaluate_states(["a", "b", "c", "d"], client, concurrency=2, limiter=fast_limiter()).read_all()
            self.assertEqual(calls, ["a", "b"])
            self.assertEqual(finished, ["b"])

    def test_cli_controls_and_quiet_ipc_output(self):
        with tempfile.TemporaryDirectory() as directory:
            states = Path(directory) / "states.txt"
            states.write_text("a\nb\nc\nd\n", encoding="utf-8")
            output = Path(directory) / "results.arrows"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("sys.argv", ["many_states.py", "--states", str(states), "--output", str(output),
                                    "--concurrency", "2", "--requests-per-minute", "60000",
                                    "--tokens-per-second", "10000000", "--quiet"]), \
                 patch("many_states.TypeSafeClient") as factory, \
                 patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                factory.return_value.__enter__.return_value = FakeClient()
                main()
            with pa.ipc.open_stream(output) as reader:
                table = reader.read_all()
            self.assertEqual(table.column("state").to_pylist(), ["a", "b", "c"])
            self.assertIn("3 HTTP attempts", stdout.getvalue())
            self.assertNotIn('"state":', stdout.getvalue())
            self.assertIn("Up to 2 concurrent", stderr.getvalue())
            self.assertIn("950 requests/s", stderr.getvalue())

    def test_cli_rejects_invalid_controls_before_opening_client(self):
        for flags in (["--concurrency", "0"], ["--max-retries", "-1"],
                      ["--requests-per-minute", "0"], ["--requests-per-minute", "NaN"],
                      ["--tokens-per-second", "Infinity"]):
            with patch("sys.argv", ["many_states.py", *flags]), \
                 patch("many_states.TypeSafeClient") as client, \
                 patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
