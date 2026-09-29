"""Offline contract tests. These never instantiate a real API client."""

import json
import math
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer, SystemOneResponse, Usage

from all_types import QUESTIONS
from arrow_results import ChoiceType, NoulType, ScoreType, evaluate_states, type_for_question
from generate_states import generate


def fixture():
    return SystemOneResponse(
        model="jev-test",
        usage=Usage(input_tokens=321, output_tokens=42),
        answers={
            "department": ChoiceAnswer(
                # Preserve the reported choice, even if another probability is larger.
                choice="billing", confidence=math.nextafter(0.87, 1.0),
                probabilities={"other": 0.01, "billing": 0.48, "returns": 0.50, "shipping": 0.01},
            ),
            "urgency": ScoreAnswer(
                score=1.2345678901234567, confidence=math.nextafter(0.65, 1.0),
                legend={i: QUESTIONS["urgency"].criteria[i] for i in (2, 0, 1)},
                probabilities={2: 0.25, 0: 0.125, 1: 0.625},
            ),
            "refund": NoulAnswer(noul=math.nextafter(0.99, 1.0)),
        },
    )


class FakeClient:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = iter(responses) if responses is not None else None

    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        if self.responses is None:
            return fixture()
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class ArrowResultsTests(unittest.TestCase):
    def test_lazy_calls_and_identical_questions(self):
        client = FakeClient()
        consumed = []

        def states():
            for state in ["one", "two", "three"]:
                consumed.append(state)
                yield state

        with evaluate_states(states(), client, batch_size=2) as reader:
            self.assertIsInstance(reader, pa.RecordBatchReader)
            self.assertEqual(client.calls, [])
            self.assertEqual(consumed, [])
            self.assertEqual(reader.schema.names, ["state", "department", "urgency", "refund"])
            self.assertEqual(reader.read_next_batch().num_rows, 2)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(consumed, ["one", "two"])
            self.assertEqual(reader.read_next_batch().num_rows, 1)
            with self.assertRaises(StopIteration):
                reader.read_next_batch()
        self.assertEqual([c["state"] for c in client.calls], ["one", "two", "three"])
        self.assertTrue(all(c["questions"] is client.calls[0]["questions"] for c in client.calls))
        self.assertEqual(client.calls[0]["questions"], QUESTIONS)

    def test_full_answer_roundtrip_and_precision(self):
        response = fixture()
        table = evaluate_states(["a"], FakeClient([response])).read_all()
        row = table.to_pylist()[0]
        for name, answer in response.answers.items():
            self.assertEqual(row[name], answer.model_dump())
        # These values do not round-trip through float32, but must survive Arrow exactly.
        self.assertEqual(struct.pack("!d", row["refund"]["noul"]), struct.pack("!d", response.nouls["refund"].noul))
        self.assertEqual(struct.pack("!d", row["urgency"]["score"]), struct.pack("!d", response.scores["urgency"].score))
        self.assertEqual(row["department"]["choice"], "billing")
        self.assertTrue(all(isinstance(k, int) for k in row["urgency"]["legend"]))
        for value in (-0.0, 0.0, 1.0, math.nextafter(0.0, 1.0)):
            t = NoulType()
            actual = t.array([NoulAnswer(noul=value)])[0].as_py()["noul"]
            self.assertEqual(struct.pack("!d", actual), struct.pack("!d", value))

    def test_layout_has_no_optional_fields_offsets_or_repeated_labels(self):
        batch = evaluate_states(["a", "b"], FakeClient()).read_next_batch()
        self.assertTrue(all(not field.nullable for field in batch.schema))
        self.assertEqual([batch.column(i).nbytes for i in (1, 2, 3)], [82, 80, 16])
        self.assertEqual(sum(batch.column(i).nbytes for i in (1, 2, 3)), 2 * 89)
        for index, width in [(1, 4), (2, 3)]:
            column = batch.column(index)
            self.assertTrue(all(not field.nullable for field in column.type.storage_type))
            probabilities = column.storage.field("probabilities")
            self.assertTrue(pa.types.is_fixed_size_list(probabilities.type))
            self.assertEqual(probabilities.type.list_size, width)
            self.assertFalse(probabilities.type.value_field.nullable)
            self.assertEqual(probabilities.type.value_type, pa.float64())
            self.assertIsNone(column.storage.buffers()[0])
            self.assertIsNone(probabilities.buffers()[0])
            self.assertIsNone(probabilities.values.buffers()[0])
        self.assertEqual(batch.column(1).storage.field("choice").type, pa.uint8())
        self.assertEqual(batch.column(3).storage.type, pa.float64())

    def test_ipc_roundtrip_in_fresh_process_and_schema_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.arrow"
            with evaluate_states(["a", "b", "c"], FakeClient(), batch_size=2) as reader:
                schema = reader.schema
                with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_stream(sink, schema) as writer:
                    for batch in reader:
                        writer.write_batch(batch)
            with pa.ipc.open_stream(path) as reader:
                self.assertTrue(reader.schema.equals(schema, check_metadata=True))
                self.assertEqual([b.num_rows for b in reader], [2, 1])
            # First inspect the portable schema WITHOUT registering extensions.
            script = """
import json, sys
import pyarrow as pa
with pa.ipc.open_stream(sys.argv[1]) as reader:
    assert reader.schema.metadata is None
    fields = list(reader.schema)[1:]
    for field in fields:
        assert b'ARROW:extension:name' in field.metadata
        info = json.loads(field.metadata[b'ARROW:extension:metadata'])
        assert info['version'] == 2
        assert 'question' not in info
        assert 'instructions' not in info
import arrow_results
with pa.ipc.open_stream(sys.argv[1]) as reader:
    assert all(isinstance(f.type, pa.ExtensionType) for f in list(reader.schema)[1:])
    print(json.dumps(reader.read_all().to_pylist()))
"""
            result = subprocess.run([sys.executable, "-c", script, str(path)], check=True, capture_output=True, text=True)
            rows = json.loads(result.stdout)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["department"], fixture().choices["department"].model_dump())
            self.assertEqual(rows[0]["refund"], fixture().nouls["refund"].model_dump())
            # Isolated mode makes project modules unavailable to the standalone reader.
            standalone = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).with_name("read_results.py")), str(path)],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual([json.loads(line) for line in standalone.stdout.splitlines()], rows)

    def test_metadata_snapshot_and_deserialization_validation(self):
        q = QUESTIONS["department"].model_copy(deep=True)
        original = type_for_question(q)
        q.criteria["returns"] = "changed after schema construction"
        copy = original.metadata
        copy["labels"][0] = "also changed"
        self.assertEqual(original.metadata, {"version": 2, "labels": list(QUESTIONS["department"].criteria)})
        # Prompt/description changes do not change the encoded answer type.
        self.assertEqual(original, type_for_question(q))
        restored = ChoiceType.__arrow_ext_deserialize__(original.storage_type, original.__arrow_ext_serialize__())
        self.assertEqual(restored.metadata, original.metadata)
        with self.assertRaises(ValueError):
            ChoiceType.__arrow_ext_deserialize__(pa.float32(), original.__arrow_ext_serialize__())
        metadata = original.metadata
        metadata["version"] = 3
        with self.assertRaises(ValueError):
            ChoiceType.__arrow_ext_deserialize__(original.storage_type, json.dumps(metadata).encode())
        reordered = ChoiceType(list(reversed(q.criteria)))
        self.assertEqual(original.storage_type, reordered.storage_type)
        self.assertNotEqual(original, reordered)
        self.assertFalse(pa.schema([("answer", original)]).equals(pa.schema([("answer", reordered)])))
        legend = [{"label": "low"}, {"label": "high"}]
        score = ScoreType(legend)
        legend[0]["label"] = "mutated"
        score.metadata["legend"][0]["label"] = "also mutated"
        self.assertEqual(score.legend, {0: {"label": "low"}, 1: {"label": "high"}})

    def test_schema_contains_only_shared_answer_metadata(self):
        with evaluate_states([], FakeClient()) as reader:
            self.assertIsNone(reader.schema.metadata)
            self.assertEqual(reader.schema.field("department").type.metadata,
                             {"version": 2, "labels": list(QUESTIONS["department"].criteria)})
            self.assertEqual(reader.schema.field("urgency").type.metadata,
                             {"version": 2, "legend": QUESTIONS["urgency"].criteria})
            self.assertEqual(reader.schema.field("refund").type.metadata, {"version": 2})
            choice = reader.schema.field("department").type
        metadata = choice.metadata
        metadata["question"] = QUESTIONS["department"].model_dump()
        with self.assertRaisesRegex(ValueError, "Invalid Jev answer metadata"):
            ChoiceType.__arrow_ext_deserialize__(choice.storage_type, json.dumps(metadata).encode())

    def test_schema_mismatches_are_errors(self):
        response = fixture()
        response.choices["department"].probabilities.pop("other")
        with self.assertRaisesRegex(ValueError, "probability keys"):
            evaluate_states(["a"], FakeClient([response])).read_all()
        response = fixture()
        response.scores["urgency"].legend[0] = "different rubric"
        with self.assertRaisesRegex(ValueError, "legend changed"):
            evaluate_states(["a"], FakeClient([response])).read_all()
        response = fixture()
        response.answers.pop("refund")
        with self.assertRaisesRegex(ValueError, "answer names"):
            evaluate_states(["a"], FakeClient([response])).read_all()
        with self.assertRaises(TypeError):
            ScoreType(QUESTIONS["urgency"].criteria).array([NoulAnswer(noul=0.5)])

    def test_empty_input_errors_and_partial_consumption(self):
        client = FakeClient()
        self.assertEqual(evaluate_states([], client).read_all().num_rows, 0)
        self.assertEqual(client.calls, [])
        for size in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                evaluate_states([], client, batch_size=size)
        with self.assertRaises(TypeError):
            evaluate_states("not a sequence of messages", client)
        for state in ("", "   ", None):
            with self.assertRaises(ValueError):
                evaluate_states([state], client).read_all()
        self.assertEqual(client.calls, [])
        client = FakeClient([fixture(), RuntimeError("API failure")])
        with evaluate_states(["a", "b", "c"], client, batch_size=1) as reader:
            self.assertEqual(reader.read_next_batch().num_rows, 1)
            with self.assertRaisesRegex(RuntimeError, "API failure"):
                reader.read_next_batch()
        self.assertEqual(len(client.calls), 2)

    def test_corpus_is_reproducible_and_unique(self):
        lines = Path(__file__).with_name("states.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 10_000)
        self.assertEqual(len(set(lines)), 10_000)
        self.assertTrue(all(line.strip() and "\t" not in line for line in lines))
        self.assertEqual(lines, [row[0] for row in generate()])


if __name__ == "__main__":
    unittest.main()
