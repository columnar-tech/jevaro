import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx2
import pyarrow as pa
import uvicorn
from jevaro import AsyncTypeSafeClient, Choice, Noul, Score, TypeSafeClient
from jevaro_server.app import create_app
from jevaro_server.arrow import schema_for

from fake_upstream import Upstream


QUESTIONS = {
    "department": Choice(instructions="Which department?", criteria={"returns": None, "other": None}),
    "urgency": Score(instructions="Urgency?", criteria=[{"when": "later"}, ["today"]]),
    "refund": Noul(instructions="Refund?"),
}
RAW_QUESTIONS = {name: q.model_dump(mode="json") for name, q in QUESTIONS.items()}


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.base_url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        with patch.dict(os.environ):
            os.environ.pop("JEVARO_MAX_RETRIES", None)  # Test the default retry count.
            app = create_app(client_factory=self.upstream.client, transport=self.upstream.transport,
                             concurrency=3)
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        self.until(lambda: self.server.started)
        self.client = TypeSafeClient(api_key="jevaro-test-key", base_url=self.base_url)

    def tearDown(self):
        self.upstream.gate.set()
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.socket.close()
        self.assertFalse(self.thread.is_alive(), "HTTP server failed to shut down")

    def until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for test condition")
            time.sleep(0.005)

    def test_schema_before_any_answers_and_bounded_cancellation(self):
        reader = self.client.system_one(states=[{"id": i, "gate": True} for i in range(1000)], questions=QUESTIONS)
        self.assertIsInstance(reader, pa.RecordBatchReader)
        self.assertEqual(reader.schema.names, list(QUESTIONS))
        self.assertEqual(next(reader).num_rows, 0)
        self.until(lambda: self.upstream.active == 3)
        self.assertEqual(self.upstream.finished, [])
        self.assertEqual(len(self.upstream.started), 3)
        reader.close()
        self.until(lambda: self.upstream.active == 0)
        self.assertEqual(len(self.upstream.cancelled), 3)

    def test_order_types_metadata_and_precision(self):
        states = [{"id": i, "delay": 0.08 if i == 0 else 0.001} for i in range(7)]
        with self.client.system_one(states=states, questions=QUESTIONS) as reader:
            table = reader.read_all()
        self.assertEqual(table.num_rows, 7)
        self.assertEqual(table["refund"].to_pylist(), [i / 1000 for i in range(7)])
        self.assertNotEqual(self.upstream.finished, list(range(7)))
        self.assertLessEqual(self.upstream.peak, 3)
        self.assertTrue(all(self.upstream.auth_matches))
        self.assertIsNone(table.schema.metadata)
        expected_metadata = {
            "department": {"labels": ["returns", "other"]},
            "urgency": {"legend": [{"when": "later"}, ["today"]]},
            "refund": {},
        }
        for field in table.schema:
            self.assertFalse(field.nullable)
            metadata = json.loads(field.metadata[b"ARROW:extension:metadata"])
            self.assertEqual(metadata, expected_metadata[field.name])
        for field in schema_for(QUESTIONS):
            extension = field.type
            self.assertEqual(type(extension).__arrow_ext_deserialize__(
                extension.storage_type, extension.__arrow_ext_serialize__(),
            ), extension)
        choice = table.schema.field("department")
        self.assertEqual(choice.type.field("choice").type, pa.uint8())
        self.assertFalse(choice.type.field("probabilities").nullable)
        self.assertFalse(choice.type.field("probabilities").type.value_field.nullable)
        self.assertEqual(choice.type.field("probabilities").type.list_size, 2)
        self.assertEqual(table["department"][0].as_py()["confidence"], 0.12345678901234567)
        self.assertEqual(table["urgency"][0].as_py()["score"], 0.9876543210987654)
        self.assertEqual(json.loads(table.schema.field("urgency").metadata[b"ARROW:extension:metadata"])["legend"],
                         [{"when": "later"}, ["today"]])

    def test_headers_and_single_array_state(self):
        with httpx2.Client() as client:
            with client.stream("POST", f"{self.base_url}/v1/systemone", headers={"Authorization": "Bearer jevaro-test-key"},
                               json={"state": ["a", "b"], "questions": RAW_QUESTIONS}) as response:
                self.assertEqual(response.headers["content-type"], "application/vnd.apache.arrow.stream")
                self.assertNotIn("content-length", response.headers)
                self.assertEqual(response.headers["transfer-encoding"], "chunked")
                self.assertEqual(response.headers["x-jevaro-row-count"], "1")
                self.assertEqual(pa.ipc.open_stream(response.read()).read_all().num_rows, 1)
        self.assertEqual(self.upstream.started, [["a", "b"]])

    def test_validation_before_upstream_and_server_key_fallback(self):
        with httpx2.Client(base_url=self.base_url) as client:
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "jevaro-test-key"}):
                good = client.post("/v1/systemone", json={"state": "one", "questions": RAW_QUESTIONS})
                self.assertEqual(good.status_code, 200)
            self.assertTrue(all(self.upstream.auth_matches))
            self.upstream.started.clear()
            for body in ({}, {"states": []}, {"states": None}, {"state": None}, {"states": "bad"},
                         {"states": [1]}, {"state": "a", "states": ["b"]}, {"state": "a", "extra": True}):
                response = client.post("/v1/systemone", json={"questions": RAW_QUESTIONS, **body})
                self.assertEqual(response.status_code, 422, body)
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                response = client.post("/v1/systemone", json={"state": "a", "questions": RAW_QUESTIONS})
                self.assertEqual(response.status_code, 401)
            self.assertEqual(self.upstream.started, [])

    def test_requests_share_one_upstream_pool_until_shutdown(self):
        for state in ("one", "two"):
            with self.client.system_one(state=state, questions=QUESTIONS) as reader:
                self.assertEqual(reader.read_all().num_rows, 1)
        first, second = self.upstream.http_clients
        self.assertIs(first, second)
        self.assertFalse(first.is_closed)
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.assertTrue(first.is_closed)

    def test_question_ids_have_no_reserved_names(self):
        questions = {name: Noul(instructions="Refund?") for name in ("state", "model", "usage")}
        with self.client.system_one(state="one", questions=questions) as reader:
            self.assertEqual(reader.read_all().schema.names, list(questions))

    def test_429_and_529_retry_in_place(self):
        with self.client.system_one(states=[{"id": 11, "statuses": [429, 529]}, {"id": 12}], questions=QUESTIONS) as reader:
            self.assertEqual(reader.read_all()["refund"].to_pylist(), [0.011, 0.012])
        self.assertEqual(self.upstream.attempts[11], 3)

    def test_five_retries_by_default_then_abort(self):
        with self.client.system_one(state={"id": 21, "statuses": [503] * 5}, questions=QUESTIONS) as reader:
            self.assertEqual(reader.read_all()["refund"].to_pylist(), [0.021])
        self.assertEqual(self.upstream.attempts[21], 6)
        with self.client.system_one(state={"id": 22, "statuses": [503] * 6}, questions=QUESTIONS) as reader:
            with self.assertRaises((httpx2.HTTPError, OSError, pa.ArrowInvalid)):
                reader.read_all()
        self.assertEqual(self.upstream.attempts[22], 6)

    def test_max_retries_validation(self):
        create_app(max_retries=0)
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                create_app(max_retries=value)
        with patch.dict(os.environ, {"JEVARO_MAX_RETRIES": "-1"}), self.assertRaises(ValueError):
            create_app()

    def test_finished_rows_share_a_batch(self):
        states = [{"id": 0, "delay": 0.1}, {"id": 1}, {"id": 2}]
        with self.client.system_one(states=states, questions=QUESTIONS) as reader:
            batches = list(reader)
        self.assertEqual([batch.num_rows for batch in batches], [0, 3])
        self.assertEqual(pa.Table.from_batches(batches)["refund"].to_pylist(), [0.0, 0.001, 0.002])

    def test_failure_aborts_after_sending_the_rows_before_it(self):
        states = [{"id": 0, "delay": 0.1}, {"id": 1}, {"id": 2, "statuses": [401]}]
        with self.client.system_one(states=states, questions=QUESTIONS) as reader:
            self.assertEqual(next(reader).num_rows, 0)
            self.assertEqual(next(reader)["refund"].to_pylist(), [0.0, 0.001])
            with self.assertRaises((httpx2.HTTPError, OSError, pa.ArrowInvalid)):
                next(reader)
        self.assertEqual(self.upstream.attempts[2], 1)

    def test_async_client(self):
        async def run():
            async with AsyncTypeSafeClient(api_key="jevaro-test-key", base_url=self.base_url) as client:
                async with await client.system_one(states=[{"id": 7}, {"id": 8}], questions=QUESTIONS) as reader:
                    self.assertEqual(reader.schema.names, list(QUESTIONS))
                    table = await reader.read_all()
                    self.assertEqual(table["refund"].to_pylist(), [0.007, 0.008])
        asyncio.run(run())

    def test_async_cancellation_closes_http_and_upstream_work(self):
        async def run():
            async with AsyncTypeSafeClient(api_key="jevaro-test-key", base_url=self.base_url, timeout=2) as client:
                async with await client.system_one(state={"id": 9, "gate": True}, questions=QUESTIONS) as reader:
                    await reader.read_next_batch()
                    reading = asyncio.create_task(reader.read_next_batch())
                    await asyncio.sleep(0.03)
                    started = time.monotonic()
                    reading.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await reading
                    self.assertLess(time.monotonic() - started, 1.0, "Cancellation waited for the read timeout")
                    while self.upstream.active and time.monotonic() - started < 1:
                        await asyncio.sleep(0.005)
                    self.assertEqual(self.upstream.active, 0, "Upstream work survived cancellation")
        asyncio.run(run())
        self.until(lambda: self.upstream.active == 0)
        self.assertEqual(self.upstream.cancelled, [9])

    def test_javascript_sdk_over_http(self):
        javascript = Path(__file__).resolve().parents[2] / "jevaro-javascript"
        result = subprocess.run(
            ["node", "--test", "test/integration.test.cjs"], cwd=javascript,
            env={**os.environ, "JEVARO_TEST_URL": self.base_url},
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.until(lambda: self.upstream.active == 0)
        self.assertTrue(all(self.upstream.auth_matches))

    def test_examples_save_and_decode(self):
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, "TYPESAFE_BASE_URL": self.base_url,
               "TYPESAFE_API_KEY": "jevaro-test-key"}
        with tempfile.TemporaryDirectory(prefix="jevaro-example-") as directory:
            output = Path(directory) / "results.arrows"
            result = subprocess.run(
                [sys.executable, str(root / "jevaro-python/example.py"), "--output", str(output)],
                env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 3)
            with pa.ipc.open_stream(output) as reader:
                self.assertEqual(reader.read_all().num_rows, 3)
            decoded = subprocess.run(
                [sys.executable, str(root / "scripts/read_results.py"), str(output)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(decoded.returncode, 0, decoded.stderr)
            rows = [json.loads(line) for line in decoded.stdout.splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["department"]["choice"], "returns")
            self.assertEqual(rows[0]["urgency"]["score"], 0.9876543210987654)
            self.assertEqual(rows[0]["refund"], {"type": "noul", "noul": 0.0})
        result = subprocess.run(
            ["node", str(root / "jevaro-javascript/example.mjs")],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["department"], "returns")
        self.assertTrue(all(self.upstream.auth_matches))


if __name__ == "__main__":
    unittest.main()
