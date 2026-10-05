"""Unit tests for rows_per_call packing: no HTTP server and no API calls."""

import asyncio
import json
import random
import unittest
from unittest.mock import patch

import httpx2
from typesafe_sdk import Choice, Noul, Score, TypeSafeAPIError
from typesafe_sdk import SystemOneResponse

from jevaro_server.packing import (
    CALL_OVERHEAD_TOKENS, PackingError, format_path, lift, packed_responses, parse_path, plan, send, state_checker,
    templates_for,
)

STATE = {"ticket": {"body": "Refund me", "odd key": 1}, "lines": [{"sku": "A-1"}]}


def answers_for(questions):
    """A TypeSafe-style response for packed questions; a Noul's answer is its row's id / 1000."""
    answers = {}
    for key, question in questions.items():
        if question["type"] == "noul":
            state = question["instructions"]["state"]
            answers[key] = {"type": "noul", "noul": state.get("id", 0) / 1000 if isinstance(state, dict) else 0.5}
        elif question["type"] == "choice":
            labels = list(question["criteria"])
            answers[key] = {"type": "choice", "choice": labels[0], "confidence": 0.5,
                            "probabilities": {label: float(i == 0) for i, label in enumerate(labels)}}
        else:
            levels = question["criteria"]
            answers[key] = {"type": "score", "score": 0.5, "confidence": 0.5,
                            "legend": {str(i): level for i, level in enumerate(levels)},
                            "probabilities": {str(i): 1 / len(levels) for i in range(len(levels))}}
    return SystemOneResponse.model_validate_json(json.dumps(
        {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 0}}))


class FakeClient:
    """Answers packed calls after a random delay; rejects calls with more than max_questions questions."""

    def __init__(self, max_questions=None, status=422, seed=0):
        self.calls = []
        self.max_questions = max_questions
        self.status = status
        self.active = 0
        self.peak = 0
        self.random = random.Random(seed)

    async def system_one(self, *, state, questions, model):
        self.calls.append(questions)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.random.uniform(0, 0.01))
            if self.max_questions is not None and len(questions) > self.max_questions:
                raise TypeSafeAPIError(self.status, {"error": "too large"}, httpx2.Headers())
            assert state == ""
            return answers_for(questions)
        finally:
            self.active -= 1


class PathTests(unittest.TestCase):
    def test_round_trip(self):
        for text in ["ticket", "ticket.messages[0].text", "[2].text", 'ticket["odd key"]', "a-b.c_d[10]"]:
            self.assertEqual(format_path(parse_path(text)), text)

    def test_not_a_path(self):
        for text in ["", "two words", "ticket..text", "ticket.", "x[abc]", "x[1", "1abc"]:
            self.assertIsNone(parse_path(text), text)


class LiftTests(unittest.TestCase):
    def lifted(self, question, state=STATE, name="q"):
        (template,) = templates_for({name: question})
        (call,) = plan([state], [template], rows_per_call=2)
        return call.questions["0.0"], call.units["0.0"]

    def test_string_instructions_are_wrapped(self):
        question, unit = self.lifted(Noul(instructions="Refund?"), "Please refund me.")
        self.assertEqual(question, {"type": "noul", "instructions": {"state": "Please refund me.", "question": "Refund?"}})
        self.assertEqual(unit, (0, "q", None))

    def test_object_list_and_missing_instructions(self):
        question, _ = self.lifted(Noul(instructions={"question": "Refund?", "note": "Ignore tone."}))
        self.assertEqual(question["instructions"], {"state": STATE, "question": "Refund?", "note": "Ignore tone."})
        self.assertEqual(list(question["instructions"]), ["state", "question", "note"])
        question, _ = self.lifted(Noul(instructions=["Refund?", "Ignore tone."]))
        self.assertEqual(question["instructions"], {"state": STATE, "question": ["Refund?", "Ignore tone."]})
        question, _ = self.lifted(Noul())
        self.assertEqual(question, {"type": "noul", "instructions": {"state": STATE}})

    def test_paths_that_resolve_are_rewritten(self):
        question, _ = self.lifted(Noul(instructions=(
            "Does `ticket.body` match `lines[0].sku`, `ticket[\"odd key\"]`, `lines[3]`, `missing`, or `two words`?")))
        self.assertEqual(question["instructions"]["question"], (
            "Does `state.ticket.body` match `state.lines[0].sku`, `state.ticket[\"odd key\"]`, `lines[3]`, "
            "`missing`, or `two words`?"))

    def test_criteria_are_rewritten_but_choice_options_are_not(self):
        question, _ = self.lifted(Choice(instructions="Team?", criteria={
            "`ticket.body`": "When `ticket.body` asks for money back", "other": None}))
        self.assertEqual(question["criteria"], {
            "`ticket.body`": "When `state.ticket.body` asks for money back", "other": None})
        question, _ = self.lifted(Noul(instructions="Refund?", criteria={"true": {"what": "`ticket.body` asks"}}))
        self.assertEqual(question["criteria"], {"true": {"what": "`state.ticket.body` asks"}})

    def test_score_legend_is_restored_only_when_levels_change(self):
        levels = ["`ticket.body` is calm", {"summary": "`ticket.body` is urgent"}]
        question, (_, _, legend) = self.lifted(Score(instructions="How urgent?", criteria=levels))
        self.assertEqual(question["criteria"], ["`state.ticket.body` is calm", {"summary": "`state.ticket.body` is urgent"}])
        self.assertEqual(legend, dict(enumerate(levels)))
        _, (_, _, legend) = self.lifted(Score(instructions="How urgent?", criteria=levels), "plain text")
        self.assertIsNone(legend)

    def test_instructions_fields_are_left_alone(self):
        question, _ = self.lifted(Noul(instructions={"policy": "30 days.", "question": "Is `ticket.body` covered by `policy`?"}))
        self.assertEqual(question["instructions"]["question"], "Is `state.ticket.body` covered by `policy`?")

    def test_string_states_have_no_paths(self):
        question, _ = self.lifted(Noul(instructions="Does `ticket.body` ask?"), "Refund me")
        self.assertEqual(question["instructions"]["question"], "Does `ticket.body` ask?")

    def test_lift_keeps_other_fields(self):
        wire = {"type": "noul", "instructions": "Refund?", "criteria": None}
        self.assertEqual(lift(wire, "a", {}), {"type": "noul", "instructions": {"state": "a", "question": "Refund?"}, "criteria": None})


class ConflictTests(unittest.TestCase):
    def test_a_state_field_in_instructions_is_rejected(self):
        with self.assertRaisesRegex(PackingError, "Question 'q' has a 'state' field in its instructions"):
            templates_for({"q": Noul(instructions={"state": "CA", "question": "West coast?"})})

    def test_an_ambiguous_field_name_is_rejected_per_row(self):
        templates = templates_for({"q": Noul(instructions={"policy": "30 days.", "question": "Covered by `policy`?"})})
        check = state_checker(templates)
        check({"ticket": "a"}, 0)
        check("policy text", 1)
        with self.assertRaisesRegex(PackingError, r"Row 2: question 'q' refers to `policy`, and 'policy' names a field"):
            check({"policy": "60 days."}, 2)

    def test_no_check_without_references_to_instructions_fields(self):
        templates = templates_for({"q": Noul(instructions={"policy": "30 days.", "question": "Covered?"})})
        self.assertIsNone(state_checker(templates))


QUESTIONS = {
    "department": Choice(instructions="Which department?", criteria={"returns": None, "other": None}),
    "urgency": Score(instructions="How urgent is `ticket.body`?", criteria=["`ticket.body` can wait", "now"]),
    "refund": Noul(instructions="Refund?"),
}


def budget(states, rows):
    """A token budget that fits about this many rows' questions, so rows split across calls."""
    one_row = next(plan(states[:1], templates_for(QUESTIONS), rows_per_call=1)).tokens
    return CALL_OVERHEAD_TOKENS + (one_row - CALL_OVERHEAD_TOKENS) * rows


class PlanTests(unittest.TestCase):
    def test_rows_per_call(self):
        calls = list(plan([{"id": i} for i in range(7)], templates_for(QUESTIONS), rows_per_call=3))
        self.assertEqual([(c.first_row, c.last_row, c.rows, c.ends_row) for c in calls],
                         [(0, 2, 3, True), (3, 5, 3, True), (6, 6, 1, True)])
        self.assertEqual(list(calls[1].questions), [f"{row}.{q}" for row in range(3) for q in range(3)])
        self.assertEqual(calls[1].units["2.1"][:2], (5, "urgency"))

    def test_a_row_continues_in_the_next_call_when_the_budget_is_reached(self):
        templates = templates_for(QUESTIONS)
        states = [{"id": i, "ticket": {"body": "x" * 200}} for i in range(3)]
        calls = list(plan(states, templates, rows_per_call=256, max_tokens=budget(states, 1.4)))
        units = [(row, name) for call in calls for row, name, _ in call.units.values()]
        self.assertEqual(units, [(row, name) for row in range(3) for name in QUESTIONS])
        self.assertGreater(len(calls), 2)
        for call in calls:
            rows = [row for row, _, _ in call.units.values()]
            self.assertEqual((call.first_row, call.last_row), (rows[0], rows[-1]))
            names = [name for row, name, _ in call.units.values() if row == call.last_row]
            self.assertEqual(call.ends_row, names[-1] == "refund")
        self.assertFalse(all(call.ends_row for call in calls))

    def test_one_large_unit_still_gets_a_call(self):
        calls = list(plan([{"text": "x" * 1000}], templates_for({"q": Noul(instructions="Long?")}), 4, max_tokens=10))
        self.assertEqual(len(calls), 1)


class SendTests(unittest.TestCase):
    def call(self, rows=4):
        states = [{"id": i, "ticket": {"body": "b"}} for i in range(rows)]
        (call,) = plan(states, templates_for(QUESTIONS), rows_per_call=rows)
        return call

    def test_rejected_calls_are_split_until_they_fit(self):
        client = FakeClient(max_questions=3)
        answers = asyncio.run(send(client, self.call(), "jev-latest"))
        self.assertEqual(sorted(answers), sorted((row, name) for row in range(4) for name in QUESTIONS))
        self.assertEqual([len(questions) for questions in client.calls[:3]], [12, 6, 6])
        self.assertEqual(sorted(len(questions) for questions in client.calls[3:]), [3, 3, 3, 3])
        legend = answers[2, "urgency"].legend
        self.assertEqual(legend, {0: "`ticket.body` can wait", 1: "now"})

    def test_other_errors_and_single_questions_are_not_split(self):
        with self.assertRaises(TypeSafeAPIError):
            asyncio.run(send(FakeClient(max_questions=3, status=401), self.call(), "jev-latest"))
        (call,) = plan([{"id": 0}], templates_for({"q": Noul()}), rows_per_call=2)
        with self.assertRaises(TypeSafeAPIError):
            asyncio.run(send(FakeClient(max_questions=0), call, "jev-latest"))

    def test_missing_answers_are_an_error(self):
        class Missing(FakeClient):
            async def system_one(self, *, state, questions, model):
                response = await super().system_one(state=state, questions=questions, model=model)
                return response.model_copy(update={"answers": dict(list(response.answers.items())[:-1])})

        with self.assertRaisesRegex(ValueError, "no answer for row 3, question 'refund'"):
            asyncio.run(send(Missing(), self.call(), "jev-latest"))


class OrderTests(unittest.TestCase):
    def collect(self, client, states, rows_per_call, concurrency):
        async def run():
            runs = []
            async for rows in packed_responses(client, states, templates_for(QUESTIONS), "jev-latest",
                                               concurrency, rows_per_call):
                runs.append(rows)
            return runs

        return asyncio.run(run())

    def test_rows_come_back_in_order_with_bounded_calls(self):
        client = FakeClient(seed=3)
        states = [{"id": i} for i in range(50)]
        runs = self.collect(client, states, rows_per_call=4, concurrency=3)
        rows = [row for run in runs for row in run]
        self.assertEqual([row.answers["refund"].noul for row in rows], [i / 1000 for i in range(50)])
        self.assertTrue(all(list(row.answers) == list(QUESTIONS) for row in rows))
        self.assertEqual(len(client.calls), 13)
        self.assertLessEqual(client.peak, 3)

    def test_rows_split_across_calls_wait_for_both(self):
        client = FakeClient(seed=5)
        states = [{"id": i, "ticket": {"body": "x" * 300}} for i in range(9)]
        with patch("jevaro_server.packing.MAX_CALL_TOKENS", budget(states, 1.4)):
            runs = self.collect(client, states, rows_per_call=256, concurrency=4)
        rows = [row for run in runs for row in run]
        self.assertEqual([row.answers["refund"].noul for row in rows], [i / 1000 for i in range(9)])
        self.assertTrue(all(row.answers["urgency"].legend == {0: "`ticket.body` can wait", 1: "now"} for row in rows))
        self.assertGreater(len(client.calls), 6)
        sizes = {len(questions) for questions in client.calls}
        self.assertNotEqual(sizes, {3})


if __name__ == "__main__":
    unittest.main()
