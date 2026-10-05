"""Offline tests for packing.py: no API calls."""

import unittest

from packing import Request, format_path, pack, parse_path, plan, rewrite, unpack
from workloads import MESSAGE_QUESTIONS, TICKET_QUESTIONS, messages, tickets

STATE = {"ticket": {"messages": [{"from": "customer", "text": "Refund me"}], "odd key": 1}, "order": {"id": "A-1"}}


def fake_response(packed):
    """An API response with one answer of the right type per packed question."""
    answers = {}
    for key, question in packed.body["questions"].items():
        if question["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.5}
        elif question["type"] == "choice":
            options = list(question["criteria"])
            answers[key] = {"type": "choice", "choice": options[0], "probabilities": {o: 1 / len(options) for o in options}, "confidence": 0.1}
        else:
            levels = question["criteria"]
            legend = {str(i): level if isinstance(level, str) else str(level) for i, level in enumerate(levels)}
            answers[key] = {"type": "score", "score": 1.0, "legend": legend, "probabilities": {str(i): 1 / len(levels) for i in range(len(levels))}, "confidence": 0.1}
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 1001, "output_tokens": 10}}


class PathTest(unittest.TestCase):
    def test_round_trip(self):
        for text in ["ticket", "ticket.messages[0].text", "[2].text", 'ticket["odd key"]', "a-b.c_d[10]"]:
            self.assertEqual(format_path(parse_path(text)), text)

    def test_not_a_path(self):
        for text in ["", "two words", "ticket..text", "ticket.", "x[abc]", "x[1", "1abc"]:
            self.assertIsNone(parse_path(text), text)

    def test_rewrites_only_paths_that_resolve(self):
        changes = {}
        text = "Does `ticket.messages[0].text` match `order.id`, `policy`, `ticket.messages[3]`, or `two words`?"
        out = rewrite(text, STATE, ["items", 4], changes=changes)
        self.assertEqual(out, "Does `items[4].ticket.messages[0].text` match `items[4].order.id`, `policy`, `ticket.messages[3]`, or `two words`?")
        self.assertEqual(changes, {"`items[4].ticket.messages[0].text`": "`ticket.messages[0].text`", "`items[4].order.id`": "`order.id`"})

    def test_quoted_keys_and_local_names(self):
        self.assertEqual(rewrite('See `ticket["odd key"]`', STATE, ["item_0"]), 'See `item_0.ticket["odd key"]`')
        self.assertEqual(rewrite("See `order.id`", STATE, ["item_0"], local={"order"}), "See `order.id`")

    def test_dict_keys_never_change(self):
        criteria = {"`order.id`": "Matches `order.id`"}
        self.assertEqual(rewrite(criteria, STATE, ["s"]), {"`order.id`": "Matches `s.order.id`"})

    def test_string_states_have_no_paths(self):
        self.assertEqual(rewrite("Is `text` urgent?", "plain text", ["items", 0]), "Is `text` urgent?")


class PackTest(unittest.TestCase):
    def test_single_sends_the_request_unchanged(self):
        requests = [Request(STATE, TICKET_QUESTIONS)]
        packed = pack(requests, [0], "single")
        self.assertEqual(packed.body, {"model": "jev-latest", "state": STATE, "questions": TICKET_QUESTIONS})

    def test_index_string_states(self):
        requests = messages()[:3]
        packed = pack(requests, [0, 1, 2], "index")
        self.assertEqual(packed.body["state"], {"items": [r.state for r in requests]})
        self.assertEqual(len(packed.body["questions"]), 9)
        q = packed.body["questions"]["2.0"]
        self.assertEqual(q["instructions"], {"question": MESSAGE_QUESTIONS["department"]["instructions"], "about": "`items[2]`"})
        self.assertEqual(list(q["criteria"]), list(MESSAGE_QUESTIONS["department"]["criteria"]))
        self.assertEqual(packed.keys["2.0"], (2, "department"))

    def test_pointer_styles(self):
        requests = messages()[:2]
        refund = "Is the customer requesting a refund?"
        prefix = pack(requests, [0, 1], "keyed", pointer="prefix").body["questions"]["1.2"]
        self.assertEqual(prefix["instructions"], f"About `item_1`: {refund}")
        scope = pack(requests, [0, 1], "index", pointer="scope").body["questions"]["1.2"]
        self.assertEqual(scope["instructions"]["question"], refund)
        self.assertIn("`items[1]`", scope["instructions"]["scope"])

    def test_index_structured_states(self):
        requests = tickets()[:2]
        body = pack(requests, [0, 1], "index").body
        department = body["questions"]["1.0"]
        self.assertEqual(department["instructions"], "Which team should handle the request in `items[1].ticket.messages[0].text`?")
        self.assertEqual(department["criteria"], TICKET_QUESTIONS["department"]["criteria"])
        urgency = body["questions"]["1.1"]["instructions"]
        self.assertEqual(urgency["inspect"], "`items[1].ticket.messages[0].text`")
        self.assertNotIn("about", urgency)
        both = body["questions"]["1.3"]["instructions"]
        self.assertIn("`items[1].ticket.messages[0].text`", both)
        self.assertIn("`items[1].order.items[0].name`", both)
        tone = body["questions"]["1.4"]["instructions"]
        self.assertEqual(tone, {"question": "How does the customer come across?", "about": "`items[1]`"})
        policy = body["questions"]["1.5"]["instructions"]
        self.assertTrue(policy["question"].endswith("covered by `policy`?"))
        self.assertIn("`items[1].ticket.messages[0].text`", policy["question"])
        always = pack(requests, [0, 1], "index", always_point=True).body["questions"]["1.0"]["instructions"]
        self.assertEqual(always["about"], "`items[1]`")

    def test_lift(self):
        requests = tickets()[:2]
        body = pack(requests, [0, 1], "lift").body
        self.assertEqual(body["state"], "")
        department = body["questions"]["0.0"]["instructions"]
        self.assertEqual(department, {"state": requests[0].state, "question": "Which team should handle the request in `state.ticket.messages[0].text`?"})
        urgency = body["questions"]["1.1"]["instructions"]
        self.assertEqual(list(urgency), ["state", "question", "inspect", "note"])
        self.assertEqual(urgency["inspect"], "`state.ticket.messages[0].text`")
        collides = [Request({"x": 1}, {"q": {"type": "noul", "instructions": {"state": "mine", "question": "Is `x` one?"}}})]
        lifted = pack(collides, [0], "lift").body["questions"]["0.0"]["instructions"]
        self.assertEqual(lifted, {"state_": {"x": 1}, "state": "mine", "question": "Is `state_.x` one?"})

    def test_animal_keys(self):
        requests = tickets()[:40]
        packed = pack(requests, list(range(8, 40)), "keyed", key_names="animals")
        names = packed.names
        self.assertEqual(len(set(names)), 32)
        self.assertEqual(list(packed.body["state"]), names)
        self.assertEqual(packed.body["state"][names[5]], requests[13].state)
        department = packed.body["questions"]["5.0"]["instructions"]
        self.assertEqual(department, f"Which team should handle the request in `{names[5]}.ticket.messages[0].text`?")
        tone = packed.body["questions"]["5.4"]["instructions"]
        self.assertEqual(tone["about"], f"`{names[5]}`")
        self.assertEqual(pack(requests, list(range(8, 40)), "keyed", key_names="animals").names, names)
        self.assertNotEqual(pack(requests, list(range(0, 32)), "keyed", key_names="animals").names, names)
        rows = unpack(packed, fake_response(packed))
        self.assertEqual(list(rows[5]["answers"]), list(TICKET_QUESTIONS))

    def test_mixed_models_are_rejected(self):
        requests = [Request("a", MESSAGE_QUESTIONS), Request("b", MESSAGE_QUESTIONS, model="jev-preview")]
        with self.assertRaises(ValueError):
            pack(requests, [0, 1], "index")


class UnpackTest(unittest.TestCase):
    def test_round_trip_every_strategy(self):
        for workload in (messages, tickets):
            requests = workload()[:5]
            for strategy in ("index", "keyed", "lift"):
                packed = pack(requests, [0, 1, 2, 3, 4], strategy)
                rows = unpack(packed, fake_response(packed))
                self.assertEqual(len(rows), 5)
                for row, request in zip(rows, requests):
                    self.assertEqual(list(row["answers"]), list(request.questions))
                self.assertEqual(sum(row["usage"]["input_tokens"] for row in rows), 1001)

    def test_score_legend_is_restored(self):
        question = {"type": "score", "instructions": "How full is `order`?", "criteria": ["`order.id` is empty", "`order.id` is set"]}
        requests = [Request(STATE, {"fill": question}), Request(STATE, {"fill": question})]
        packed = pack(requests, [0, 1], "index")
        self.assertEqual(packed.body["questions"]["1.0"]["criteria"], ["`items[1].order.id` is empty", "`items[1].order.id` is set"])
        rows = unpack(packed, fake_response(packed))
        self.assertEqual(rows[1]["answers"]["fill"]["legend"], {"0": "`order.id` is empty", "1": "`order.id` is set"})

    def test_missing_answers_raise(self):
        packed = pack(messages()[:2], [0, 1], "index")
        response = fake_response(packed)
        del response["answers"]["1.1"]
        with self.assertRaises(ValueError):
            unpack(packed, response)


class PlanTest(unittest.TestCase):
    def test_batches_split_until_they_fit(self):
        big = "x" * 20_000
        requests = [Request(big, MESSAGE_QUESTIONS) for _ in range(8)]
        calls = plan(requests, [list(range(8))], "index")
        self.assertEqual(sorted(row for call in calls for row in call.rows), list(range(8)))
        self.assertGreater(len(calls), 1)

    def test_single_makes_one_call_per_row(self):
        calls = plan(messages()[:4], [[0, 1], [2, 3]], "single")
        self.assertEqual([call.rows for call in calls], [[0], [1], [2], [3]])


if __name__ == "__main__":
    unittest.main()
