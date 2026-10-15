import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import httpx2
import pyarrow as pa
from jevaro import AsyncTypeSafeClient, Box, Choice, Image, Noul
from jevaro_server.arrow import BoxType, schema_for
from jevaro_server.images import References

from test_proxy import MEDIA_TYPE, ServerTestCase, ipc_stream

ON = {"TypeSafe-Danger-Zone-Images": "true"}
RED = b"\x89PNG\r\n\x1a\nred"
BLUE = b"\xff\xd8\xffblue"
GREEN = b"\x89PNG\r\n\x1a\ngreen"
BOX = {"type": "box", "instructions": "the mug in `photo`"}
SEEN = {"type": "noul", "instructions": "Is there a mug in `photo`?"}


def b64(data):
    return base64.b64encode(data).decode()


def table_of(content):
    return pa.ipc.open_stream(content).read_all()


class ReferenceTests(unittest.TestCase):
    def test_whole_values_then_left_to_right_without_overlaps(self):
        references = References(["ab", "abc", "bcd", "<x>"])
        for value, expected in (
            ("abc", ["abc"]),
            ("xabcd", ["abc"]),
            ("ab bcd <x>", ["ab", "bcd", "<x>"]),
            ({"ab": "no keys", "list": ["bcd", {"k": "<x><x>"}]}, ["bcd", "<x>"]),
            ("a b c", []),
        ):
            found = {}
            references.find(value, found)
            self.assertEqual(list(found), expected, value)
        found = {}
        references.find(["ab", "<image:generated>"], found, skip={"<image:generated>": b"x"})
        self.assertEqual(list(found), ["ab"])
        found = {}
        References([]).find("anything", found)
        self.assertEqual(found, {})


class BoxSchemaTests(unittest.TestCase):
    def test_box_types_round_trip_and_differ_by_format(self):
        questions = {"mug": Box(instructions="the mug")}
        default, geoarrow = schema_for(questions).field("mug"), schema_for(questions, "geoarrow").field("mug")
        self.assertNotEqual(default.type, geoarrow.type)
        for field in (default, geoarrow):
            extension = field.type
            self.assertEqual(type(extension).__arrow_ext_deserialize__(
                extension.storage_type, extension.__arrow_ext_serialize__()), extension)
        with self.assertRaises(ValueError):
            BoxType.__arrow_ext_deserialize__(default.type.storage_type, b'{"x": 1}')
        with self.assertRaises(ValueError):
            schema_for(questions, "pixels")


class ImageTests(ServerTestCase):
    def post(self, body, headers=ON):
        with httpx2.Client(base_url=self.base_url, headers={"Authorization": "Bearer jevaro-test-key"}) as client:
            return client.post("/v1/systemone", json=body, headers=headers)

    def calls(self):
        """Each upstream call's state and images, in row order."""
        calls = [(payload["state"], images, headers)
                 for payload, images, headers in zip(self.upstream.payloads, self.upstream.images, self.upstream.headers)]
        return sorted(calls, key=lambda call: json.dumps(call[0]))

    def test_box_answers_mirror_typesafe_with_nullable_confidence(self):
        response = self.post({"states": [{"id": 1}, {"id": 2, "no_confidence": True}],
                              "questions": {"mug": BOX, "seen": SEEN}})
        self.assertEqual(response.status_code, 200, response.text)
        table = table_of(response.content)
        field = table.schema.field("mug")
        self.assertFalse(field.nullable)
        self.assertEqual(field.metadata[b"ARROW:extension:name"], b"jevaro.box")
        self.assertEqual(json.loads(field.metadata[b"ARROW:extension:metadata"]), {})
        self.assertEqual(field.type, pa.struct([
            pa.field("box", pa.struct([pa.field(edge, pa.float64(), nullable=False)
                                       for edge in ("top", "bottom", "left", "right")]), nullable=False),
            pa.field("confidence", pa.float64()),
        ]))
        self.assertEqual(table["mug"].to_pylist(), [
            {"box": {"top": 0.001, "bottom": 0.501, "left": 0.251, "right": 0.751}, "confidence": 0.875},
            {"box": {"top": 0.002, "bottom": 0.502, "left": 0.252, "right": 0.752}, "confidence": None},
        ])
        self.assertEqual(table["seen"].to_pylist(), [0.001, 0.002])

    def test_geoarrow_box_format(self):
        response = self.post({"state": {"id": 3}, "questions": {"mug": BOX}, "box_format": "geoarrow"})
        self.assertEqual(response.status_code, 200, response.text)
        table = table_of(response.content)
        box = table.schema.field("mug").type.field("box")
        self.assertEqual(box.metadata, {b"ARROW:extension:name": b"geoarrow.box", b"ARROW:extension:metadata": b"{}"})
        self.assertEqual([field.name for field in box.type], ["xmin", "ymin", "xmax", "ymax"])
        self.assertTrue(all(not field.nullable for field in box.type))
        self.assertEqual(table["mug"].to_pylist(), [
            {"box": {"xmin": 0.253, "ymin": 0.003, "xmax": 0.753, "ymax": 0.503}, "confidence": 0.875}])

    def test_danger_zone_headers_are_forwarded(self):
        headers = {**ON, "TypeSafe-Danger-Zone-Long-Context": "true", "X-Other": "no"}
        response = self.post({"states": ["a", "b"], "questions": {"seen": SEEN}}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.upstream.headers, [{"typesafe-danger-zone-images": "true",
                                                  "typesafe-danger-zone-long-context": "true"}] * 2)
        self.upstream.headers.clear()
        with self.client.system_one(states=["a"], questions={"seen": Noul(instructions="Mug?")},
                                    extra_headers={"TypeSafe-Danger-Zone-Long-Context": "true"}) as reader:
            reader.read_all()
        self.assertEqual(self.upstream.headers, [{"typesafe-danger-zone-long-context": "true"}])

    def test_images_and_box_need_the_images_header(self):
        cases = [
            {"state": "a", "questions": {"mug": BOX}},
            {"state": {"photo": "<red>"}, "questions": {"seen": SEEN},
             "assets": {"<red>": {"format": "png", "data_base64": b64(RED)}}},
        ]
        for body in cases:
            for headers in ({}, {"TypeSafe-Danger-Zone-Images": "1"}):
                response = self.post(body, headers=headers)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("Send the header TypeSafe-Danger-Zone-Images: true", response.text)
        response = self.post(cases[0], headers={"TypeSafe-Danger-Zone-Images": "TRUE"})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.post_form({"request": (None, json.dumps({"questions": {"seen": SEEN}}).encode(), None),
                                   "states": ("s.arrows", ipc_stream(pa.table({"photo": [RED]})), MEDIA_TYPE)})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("TypeSafe-Danger-Zone-Images", response.text)
        self.assertEqual(len(self.upstream.payloads), 1)

    def test_base64_assets_go_only_with_the_calls_that_use_them(self):
        body = {
            "states": [{"id": 1, "photo": "<red>"}, {"id": 2, "photo": "<blue>"}, {"id": 3, "photo": "none"},
                       "Shelf: <blue> beside <red>"],
            "assets": {"<red>": {"type": "image", "format": "png", "data_base64": b64(RED)},
                       "<blue>": {"format": "jpg", "data_base64": b64(BLUE)},
                       "<mug>": {"format": "jpeg", "data_base64": b64(GREEN)}},
            "questions": {"seen": SEEN, "same": {"type": "noul", "instructions": "Is `photo` this mug: <mug>?"}},
        }
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(table_of(response.content)["seen"].to_pylist(), [0.001, 0.002, 0.003, 0.0])
        self.assertEqual([(state, images) for state, images, _ in self.calls()], [
            ("Shelf: <blue> beside <red>", {"<mug>": GREEN, "<blue>": BLUE, "<red>": RED}),
            ({"id": 1, "photo": "<red>"}, {"<mug>": GREEN, "<red>": RED}),
            ({"id": 2, "photo": "<blue>"}, {"<mug>": GREEN, "<blue>": BLUE}),
            ({"id": 3, "photo": "none"}, {"<mug>": GREEN}),
        ])
        # Image files are already compressed, so their bodies are sent as they are.
        self.assertEqual(set(self.upstream.encodings), {None})

    def test_multipart_image_parts_with_json_states(self):
        request = {"states": [{"id": 4, "photo": "ref-red"}, {"id": 5, "photo": "ref-blue"}],
                   "assets": {"ref-red": {"part": "red-photo"}, "ref-blue": {"type": "image", "part": "blue-photo"}},
                   "questions": {"mug": BOX}}
        response = self.post_form([
            ("request", (None, json.dumps(request).encode(), "application/json")),
            ("red-photo", ("red.png", RED, "image/png")),
            ("blue-photo", ("blue.jpg", BLUE, None)),
        ], headers=ON)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-jevaro-row-count"], "2")
        self.assertEqual([box["box"]["top"] for box in table_of(response.content)["mug"].to_pylist()], [0.004, 0.005])
        self.assertEqual([images for _, images, _ in self.calls()], [{"ref-red": RED}, {"ref-blue": BLUE}])

    def test_arrow_binary_values_are_each_rows_images(self):
        table = pa.table({
            "id": [6, 7, 8],
            "photo": pa.array([RED, BLUE, None], pa.large_binary()),
            "frames": pa.array([[GREEN, RED], [], [BLUE]], pa.list_(pa.binary_view())),
        })
        response = self.post_form({
            "request": (None, json.dumps({"questions": {"mug": BOX, "seen": SEEN}}).encode(), "application/json"),
            "states": ("s.arrows", ipc_stream(table), MEDIA_TYPE),
        }, headers=ON)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(table_of(response.content)["seen"].to_pylist(), [0.006, 0.007, 0.008])
        calls = sorted(zip(self.upstream.payloads, self.upstream.images), key=lambda call: call[0]["state"]["id"])
        for (payload, images), (photo, frames) in zip(calls, [(RED, [GREEN, RED]), (BLUE, []), (None, [BLUE])]):
            state = payload["state"]
            self.assertEqual(set(state), {"id", "photo", "frames"})
            self.assertEqual(images[state["photo"]] if photo else state["photo"], photo)
            self.assertEqual([images[reference] for reference in state["frames"]], frames)
            self.assertEqual(len(images), (photo is not None) + len(frames))
            self.assertTrue(all(reference.startswith("<image:") and len(reference) == 40 for reference in images))
        references = [reference for _, images in calls for reference in images]
        self.assertEqual(len(set(references)), len(references))

    def test_arrow_image_state_column_and_shared_question_images(self):
        table = pa.table({"id": [9, 10], "photo": [RED, BLUE], "caption": ["Kitchen", "Shelf <mug>"]})
        request = {"questions": {"same": {"type": "choice", "instructions": "Which mug is in the state?",
                                          "criteria": {"mug": "<mug>", "none": "No mug"}}},
                   "assets": {"<mug>": {"part": "mug-photo"}}, "state_column": "photo"}
        response = self.post_form({
            "request": (None, json.dumps(request).encode(), "application/json"),
            "states": ("s.arrows", ipc_stream(table), MEDIA_TYPE),
            "mug-photo": ("mug.png", GREEN, "application/octet-stream"),
        }, headers=ON)
        self.assertEqual(response.status_code, 200, response.text)
        calls = list(zip(self.upstream.payloads, self.upstream.images))
        self.assertEqual(sorted(images[payload["state"]] for payload, images in calls), sorted([RED, BLUE]))
        self.assertTrue(all(images["<mug>"] == GREEN and len(images) == 2 for _, images in calls))
        self.upstream.payloads.clear()
        self.upstream.images.clear()
        # The caption column refers to the request's image by its reference.
        request = {"questions": {"seen": SEEN}, "assets": {"<mug>": {"part": "mug-photo"}}}
        response = self.post_form({
            "request": (None, json.dumps(request).encode(), "application/json"),
            "states": ("s.arrows", ipc_stream(table.select(["id", "caption"])), MEDIA_TYPE),
            "mug-photo": ("mug.png", GREEN, None),
        }, headers=ON)
        self.assertEqual(response.status_code, 200, response.text)
        by_id = {payload["state"]["id"]: images for payload, images in zip(self.upstream.payloads, self.upstream.images)}
        self.assertEqual(by_id, {9: {}, 10: {"<mug>": GREEN}})

    def test_image_requests_are_checked_before_upstream(self):
        red = {"format": "png", "data_base64": b64(RED)}
        json_cases = [
            ("not referenced in any state", {"state": "a", "questions": {"seen": SEEN}, "assets": {"<red>": red}}),
            ("names a part; send parts in a multipart", {"state": "<red>", "questions": {"seen": SEEN},
                                                         "assets": {"<red>": {"part": "p"}}}),
            ("invalid data_base64", {"state": "<red>", "questions": {"seen": SEEN},
                                     "assets": {"<red>": {"format": "png", "data_base64": "iVBO RW=="}}}),
            ("invalid data_base64", {"state": "<red>", "questions": {"seen": SEEN},
                                     "assets": {"<red>": {"format": "png", "data_base64": "data:image/png;base64,AA=="}}}),
            ("format is required", {"state": "<red>", "questions": {"seen": SEEN},
                                    "assets": {"<red>": {"data_base64": b64(RED)}}}),
            ("needs part or data_base64", {"state": "<red>", "questions": {"seen": SEEN}, "assets": {"<red>": {}}}),
            ("literal_error", {"state": "<red>", "questions": {"seen": SEEN},
                               "assets": {"<red>": {**red, "format": "gif"}}}),
            ("literal_error", {"state": "<red>", "questions": {"seen": SEEN}, "assets": {"<red>": {**red, "type": "video"}}}),
            ("is empty", {"state": "<red>", "questions": {"seen": SEEN},
                          "assets": {"<red>": {"format": "png", "data_base64": ""}}}),
            ("can't be empty or only whitespace", {"state": " ", "questions": {"seen": SEEN}, "assets": {" ": red}}),
            ("is over 1024 bytes", {"state": "é" * 513, "questions": {"seen": SEEN}, "assets": {"é" * 513: red}}),
            ("TypeSafe allows 65,536 per request", {"state": [f"<{i:0996}>" for i in range(66)], "questions": {"seen": SEEN},
                                    "assets": {f"<{i:0996}>": red for i in range(66)}}),
            ("rows_per_call above 1 can't be used with images", {"states": ["<red>"], "questions": {"seen": SEEN},
                                                                 "assets": {"<red>": red}, "rows_per_call": 2}),
            ("rows_per_call above 1 can't be used with images", {"states": ["a"], "questions": {"mug": BOX},
                                                                 "rows_per_call": 2}),
            ("extra_forbidden", {"state": "a", "questions": {"mug": {**BOX, "criteria": ["a"]}}}),
            ("literal_error", {"state": "a", "questions": {"mug": BOX}, "box_format": "pixels"}),
        ]
        for expected, body in json_cases:
            with self.subTest(expected):
                response = self.post(body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(expected, response.text)

        def request(**fields):
            return ("request", (None, json.dumps({"questions": {"seen": SEEN}, **fields}).encode(), None))

        form_cases = [
            ("names part 'p', which the request doesn't have", [request(state="<red>", assets={"<red>": {"part": "p"}})]),
            ("both name part 'p'", [request(state="<red> <blue>", assets={"<red>": {"part": "p"}, "<blue>": {"part": "p"}}),
                                    ("p", ("p", RED, None))]),
            ("Part 'q' is not named by any asset", [request(state="<red>", assets={"<red>": {"part": "p"}}),
                                                    ("p", ("p", RED, None)), ("q", ("q", RED, None))]),
            ("Content-Type text/plain", [request(state="<red>", assets={"<red>": {"part": "p"}}),
                                         ("p", ("p", RED, "text/plain"))]),
            ("Missing part 'states'", [request(assets={"<red>": {"part": "p"}}), ("p", ("p", RED, None))]),
            ("Missing part 'request'", [("p", ("p", RED, None))]),
            ("state_column requires Arrow states", [request(state="a", state_column="text")]),
            ("states must be a nonempty array", [request(states=[])]),
            ("state must be a string, object, or array", [request(state=None)]),
            ("Row 1: photo is an empty image", [request(), ("states", ("s.arrows", ipc_stream(
                pa.table({"photo": [RED, b""]})), MEDIA_TYPE))]),
            ("Asset '<mug>' is not referenced", [request(assets={"<mug>": {"part": "p"}}), ("p", ("p", GREEN, None)),
                                                 ("states", ("s.arrows", ipc_stream(pa.table({"photo": [RED]})), None))]),
        ]
        for expected, files in form_cases:
            with self.subTest(expected):
                response = self.post_form(files, headers=ON)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(expected, response.text)
        self.assertEqual(self.upstream.payloads, [])

    def test_python_sdk_sends_image_objects(self):
        photos = [Image(RED), Image(BLUE)]
        mug = Image(GREEN)
        questions = {"mug": Box(instructions="the mug in `photo`"),
                     "kind": Choice(instructions="Which mug is it?", criteria={"this": mug, "other": None})}
        states = [{"id": 11, "photo": photos[0]}, {"id": 12, "photo": photos[1]}]
        with self.client.system_one(states=states, questions=questions, extra_headers=ON) as reader:
            table = reader.read_all()
        self.assertEqual([row["box"]["left"] for row in table["mug"].to_pylist()], [0.261, 0.262])
        by_id = {payload["state"]["id"]: images for payload, images in zip(self.upstream.payloads, self.upstream.images)}
        self.assertEqual(by_id, {11: {str(photos[0]): RED, str(mug): GREEN}, 12: {str(photos[1]): BLUE, str(mug): GREEN}})
        self.upstream.payloads.clear()
        self.upstream.images.clear()

        async def run():
            async with AsyncTypeSafeClient(api_key="jevaro-test-key", base_url=self.base_url) as client:
                reader = await client.system_one(states=pa.table({"id": [13], "photo": [RED]}),
                                                 questions={"mug": Box(instructions="the mug in `photo`")},
                                                 box_format="geoarrow", extra_headers=ON)
                async with reader:
                    return await reader.read_all()

        table = asyncio.run(run())
        self.assertEqual(table["mug"].to_pylist(), [
            {"box": {"xmin": 0.263, "ymin": 0.013, "xmax": 0.763, "ymax": 0.513}, "confidence": 0.875}])
        (images,) = self.upstream.images
        self.assertEqual(list(images.values()), [RED])

    def test_read_results_decodes_saved_box_answers(self):
        root = Path(__file__).resolve().parents[2]
        decoded = []
        for box_format in ("typesafe", "geoarrow"):
            response = self.post({"states": [{"id": 14}, {"id": 15, "no_confidence": True}],
                                  "questions": {"mug": BOX}, "box_format": box_format})
            self.assertEqual(response.status_code, 200, response.text)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "boxes.arrows"
                path.write_bytes(response.content)
                result = subprocess.run([sys.executable, str(root / "scripts/read_results.py"), str(path)],
                                        capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            decoded.append([json.loads(line) for line in result.stdout.splitlines()])
        self.assertEqual(decoded[0], decoded[1])
        self.assertEqual(decoded[0], [
            {"mug": {"type": "box", "box": {"top": 0.014, "bottom": 0.514, "left": 0.264, "right": 0.764},
                     "confidence": 0.875}},
            {"mug": {"type": "box", "box": {"top": 0.015, "bottom": 0.515, "left": 0.265, "right": 0.765},
                     "confidence": None}},
        ])
