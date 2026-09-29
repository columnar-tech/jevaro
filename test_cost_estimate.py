"""Cost estimation tests with no network access."""

import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
import pyarrow as pa
from typesafe_sdk import TypeSafeAPIError, TypeSafeClient

from all_types import QUESTIONS
from many_states import estimate_cost, main


class FakeClient:
    def __init__(self, tokens=321, model="jev-1.13.0"):
        self.calls = []
        self.response = SimpleNamespace(model=model, usage=SimpleNamespace(input_tokens=tokens))

    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class CostEstimateTests(unittest.TestCase):
    def test_one_call_uses_near_mean_sample_and_exact_cost_arithmetic(self):
        client = FakeClient()
        report = estimate_cost(iter(["short", "medium size", "a much longer message"]), client)
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["state"], "medium size")
        self.assertEqual(call["questions"], QUESTIONS)
        self.assertEqual(call["retry"].max_retries, 0)
        self.assertEqual(report["sample_line_number"], 2)
        self.assertEqual(report["estimated_total_input_tokens"], 963)
        self.assertEqual(Decimal(report["estimated_input_cost_usd"]), Decimal("0.000040446"))
        self.assertEqual(Decimal(report["sample_request_cost_usd"]), Decimal("0.000013482"))

    def test_resolved_model_controls_price_and_override_can_be_zero(self):
        client = FakeClient(model="jev-future")
        with self.assertRaisesRegex(ValueError, "No recorded input price"):
            estimate_cost(["message"], client)
        self.assertEqual(len(client.calls), 1)
        for rate, expected in [(Decimal("1.25"), Decimal("0.00040125")), (Decimal(0), Decimal(0))]:
            report = estimate_cost(["message"], FakeClient(model="jev-future"), input_price_per_million=rate)
            self.assertEqual(Decimal(report["estimated_input_cost_usd"]), expected)
            self.assertEqual(report["pricing_source"], "user override")

    def test_invalid_states_do_not_call_api_and_missing_usage_is_not_guessed(self):
        for states in ([], [""], ["okay", "   "], [None]):
            client = FakeClient()
            with self.assertRaises(ValueError):
                estimate_cost(states, client)
            self.assertEqual(client.calls, [])
        for tokens in (None, -1, True, 10.5):
            client = FakeClient(tokens=tokens)
            with self.assertRaisesRegex(ValueError, "usage.input_tokens"):
                estimate_cost(["message"], client)
            self.assertEqual(len(client.calls), 1)

    def test_sdk_does_not_retry_a_failed_probe(self):
        attempts = []

        def handler(request):
            attempts.append(request)
            return httpx2.Response(503, json={"error": "unavailable"})

        with TypeSafeClient(api_key="offline-test", transport=httpx2.MockTransport(handler)) as client:
            with self.assertRaises(TypeSafeAPIError):
                estimate_cost(["one", "two"], client)
        self.assertEqual(len(attempts), 1)

    def test_cli_estimate_defaults_to_whole_file_and_honors_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "states.txt"
            path.write_text("a\nbb\nccc\ndddd\neeeee\n", encoding="utf-8")
            for flags, expected in [([], 5), (["--limit", "2"], 2), (["--limit", "100"], 5)]:
                client = FakeClient()
                output = io.StringIO()
                with patch("sys.argv", ["many_states.py", "--states", str(path), "--estimate-cost", *flags]), \
                     patch("many_states.TypeSafeClient") as factory, \
                     patch("many_states.evaluate_states") as evaluate, \
                     patch("sys.stdout", output):
                    factory.return_value.__enter__.return_value = client
                    main()
                self.assertEqual(json.loads(output.getvalue())["states"], expected)
                self.assertEqual(len(client.calls), 1)
                evaluate.assert_not_called()

    def test_cli_evaluation_still_defaults_to_three(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "states.txt"
            path.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
            captured = []

            def reader(states, client, **kwargs):
                captured.extend(states)
                return pa.RecordBatchReader.from_batches(pa.schema([]), [])

            with patch("sys.argv", ["many_states.py", "--states", str(path)]), \
                 patch("many_states.TypeSafeClient"), \
                 patch("many_states.evaluate_states", side_effect=reader), \
                 patch("sys.stdout", io.StringIO()):
                main()
            self.assertEqual(captured, ["a", "b", "c"])

    def test_cli_rejects_invalid_price_and_estimate_output_before_client_creation(self):
        for flags in (["--estimate-cost", "--output", "not-created.arrow"],
                      ["--input-price-per-million", "0.1"],
                      ["--estimate-cost", "--input-price-per-million", "-1"],
                      ["--estimate-cost", "--input-price-per-million", "NaN"],
                      ["--estimate-cost", "--input-price-per-million", "Infinity"]):
            with patch("sys.argv", ["many_states.py", *flags]), \
                 patch("many_states.TypeSafeClient") as factory, \
                 patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
