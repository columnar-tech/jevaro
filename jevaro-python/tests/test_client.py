import io
import json
import tempfile
import unittest
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

import httpx2
import pyarrow as pa
from jevaro import Box, Choice, Image, Noul, TypeSafeClient

PNG = b"\x89PNG\r\n\x1a\npixels"
JPEG = b"\xff\xd8\xffpixels"


def ipc(values):
    data = io.BytesIO()
    table = pa.table({"answer": pa.array(values, type=pa.float64())})
    with pa.ipc.new_stream(data, table.schema) as writer:
        writer.write_table(table)
    return data.getvalue()


class Chunks(httpx2.SyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    def __iter__(self):
        for offset in range(0, len(self.data), 7):
            yield self.data[offset:offset + 7]

    def close(self):
        self.closed = True


def form_parts(request):
    message = BytesParser(policy=HTTP).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content)
    return {part.get_param("name", header="content-disposition"):
            (part.get_content_type(), part.get_payload(decode=True)) for part in message.iter_parts()}


class ClientTests(unittest.TestCase):
    def client(self, values=(0.12345678901234567,), status=200, content_type="application/vnd.apache.arrow.stream"):
        client = TypeSafeClient(api_key="test-key")
        client._http.close()
        stream = Chunks(ipc(values))
        self.requests = []
        client._http = httpx2.Client(transport=httpx2.MockTransport(lambda request: self.requests.append(request) or httpx2.Response(
            status, stream=stream, headers={"Content-Type": content_type},
        )))
        self.addCleanup(client.close)
        return client, stream

    def test_chunk_boundaries_native_reader_and_close_on_exhaustion(self):
        client, stream = self.client()
        reader = client.system_one(state="one", questions={"answer": Noul()})
        self.assertIsInstance(reader, pa.RecordBatchReader)
        self.assertFalse(stream.closed)
        self.assertEqual(reader.read_all()["answer"].to_pylist(), [0.12345678901234567])
        self.assertTrue(stream.closed)
        self.assertFalse(client._readers)

    def test_clean_eof_with_missing_rows_is_an_error(self):
        client, stream = self.client()
        reader = client.system_one(states=["one", "two"], questions={"answer": Noul()})
        with self.assertRaisesRegex(OSError, "expected 2 rows, got 1"):
            reader.read_all()
        self.assertTrue(stream.closed)

    def test_extra_rows_are_an_error(self):
        client, stream = self.client(values=(0.1, 0.2))
        reader = client.system_one(state="one", questions={"answer": Noul()})
        with self.assertRaisesRegex(OSError, "more rows"):
            reader.read_all()
        self.assertTrue(stream.closed)

    def test_client_close_closes_unconsumed_readers(self):
        client, stream = self.client()
        client.system_one(state="one", questions={"answer": Noul()})
        client.close()
        self.assertTrue(stream.closed)

    def test_http_errors_close_body(self):
        client, stream = self.client(status=422)
        with self.assertRaises(httpx2.HTTPStatusError):
            client.system_one(state="one", questions={"answer": Noul()})
        self.assertTrue(stream.closed)

    def test_wrong_media_type_closes_body(self):
        client, stream = self.client(content_type="application/json")
        with self.assertRaisesRegex(ValueError, "Arrow IPC stream"):
            client.system_one(state="one", questions={"answer": Noul()})
        self.assertTrue(stream.closed)

    def test_arrow_states_are_sent_as_a_multipart_form(self):
        client, _ = self.client(values=(0.1, 0.2))
        table = pa.table({"id": [1, 2], "text": ["one", "two"]})
        with client.system_one(states=table, state_column="text", questions={"answer": Noul()}) as reader:
            self.assertEqual(reader.read_all().num_rows, 2)
        request, = self.requests
        self.assertTrue(request.headers["content-type"].startswith("multipart/form-data; boundary="))
        parts = form_parts(request)
        self.assertEqual(list(parts), ["request", "states"])
        self.assertEqual(parts["request"][0], "application/json")
        self.assertEqual(json.loads(parts["request"][1]), {
            "model": "jev-latest", "questions": {"answer": Noul().model_dump(mode="json")},
            "state_column": "text",
        })
        self.assertEqual(parts["states"][0], "application/vnd.apache.arrow.stream")
        self.assertTrue(pa.ipc.open_stream(parts["states"][1]).read_all().equals(table))

    def test_rows_per_call_is_sent_in_json_and_in_the_request_part(self):
        client, _ = self.client(values=(0.1, 0.2))
        with client.system_one(states=["one", "two"], questions={"answer": Noul()}, rows_per_call=32) as reader:
            reader.read_all()
        self.assertEqual(json.loads(self.requests[0].content)["rows_per_call"], 32)
        client, _ = self.client(values=(0.1, 0.2))
        table = pa.table({"text": ["one", "two"]})
        with client.system_one(states=table, questions={"answer": Noul()}, rows_per_call=8) as reader:
            reader.read_all()
        self.assertEqual(json.loads(form_parts(self.requests[0])["request"][1])["rows_per_call"], 8)
        client, _ = self.client()
        with client.system_one(state="one", questions={"answer": Noul()}) as reader:
            reader.read_all()
        self.assertNotIn("rows_per_call", json.loads(self.requests[0].content))

    def test_arrow_row_count_comes_from_the_table(self):
        client, stream = self.client()
        reader = client.system_one(states=pa.table({"text": ["one", "two"]}), questions={"answer": Noul()})
        with self.assertRaisesRegex(OSError, "expected 2 rows, got 1"):
            reader.read_all()
        self.assertTrue(stream.closed)

    def test_images_are_sent_as_file_parts_named_by_assets(self):
        client, _ = self.client(values=(0.1, 0.2))
        photo, badge, thumb = Image(PNG), Image(bytearray(JPEG)), Image(b"other")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logo.png"
            path.write_bytes(PNG + b"logo")
            questions = {
                "mug": Box(instructions="the mug in `photo`"),
                "same": Noul(instructions={"question": "Is `photo` the person in `badge`?", "badge": badge}),
                "gesture": {"type": "choice", "instructions": "Which gesture?", "criteria": {"up": thumb, "none": None}},
            }
            with client.system_one(states=[{"photo": photo}, "Logo: <logo>"], questions=questions,
                                   assets={"<logo>": Image(path)}, box_format="geoarrow") as reader:
                reader.read_all()
        parts = form_parts(self.requests[0])
        self.assertEqual(list(parts), ["request", "asset-0", "asset-1", "asset-2", "asset-3"])
        request = json.loads(parts["request"][1])
        self.assertEqual(request["states"], [{"photo": str(photo)}, "Logo: <logo>"])
        self.assertEqual(request["questions"]["same"]["instructions"]["badge"], str(badge))
        self.assertEqual(request["questions"]["gesture"]["criteria"], {"up": str(thumb), "none": None})
        self.assertEqual(request["questions"]["mug"], {"type": "box", "instructions": "the mug in `photo`"})
        self.assertEqual(request["box_format"], "geoarrow")
        # Named assets first, then the images in the questions, then those in the states.
        self.assertEqual(request["assets"], {
            "<logo>": {"type": "image", "part": "asset-0"}, str(badge): {"type": "image", "part": "asset-1"},
            str(thumb): {"type": "image", "part": "asset-2"}, str(photo): {"type": "image", "part": "asset-3"},
        })
        self.assertEqual([parts[f"asset-{i}"] for i in range(4)], [
            ("image/png", PNG + b"logo"), ("image/jpeg", JPEG), ("application/octet-stream", b"other"),
            ("image/png", PNG),
        ])

    def test_images_with_arrow_states_and_without_images(self):
        client, _ = self.client(values=(0.1, 0.2))
        table = pa.table({"photo": [PNG, JPEG]})
        mug = Image(PNG)
        questions = {"same": Choice(instructions="Which mug?", criteria={"this": mug, "none": None})}
        with client.system_one(states=table, questions=questions) as reader:
            reader.read_all()
        parts = form_parts(self.requests[0])
        self.assertEqual(list(parts), ["request", "states", "asset-0"])
        self.assertEqual(json.loads(parts["request"][1])["assets"], {str(mug): {"type": "image", "part": "asset-0"}})
        self.assertTrue(pa.ipc.open_stream(parts["states"][1]).read_all().equals(table))
        client, _ = self.client()
        with client.system_one(state={"text": "é"}, questions={"answer": Noul()}) as reader:
            reader.read_all()
        self.assertEqual(self.requests[0].headers["content-type"], "application/json")
        self.assertEqual(json.loads(self.requests[0].content)["state"], {"text": "é"})

    def test_invalid_arguments(self):
        client, _ = self.client()
        for args in ({}, {"states": []}, {"states": "no"}, {"state": "a", "states": ["b"]},
                     {"states": {"text": ["a"]}}, {"states": ["a"], "state_column": "text"},
                     {"state": "a", "state_column": "text"},
                     {"states": pa.table({"text": pa.array([], pa.string())})},
                     {"states": pa.chunked_array([[1, 2]])},
                     {"states": ["a"], "rows_per_call": 0}, {"states": ["a"], "rows_per_call": True},
                     {"states": ["a"], "rows_per_call": 2.0},
                     {"state": "a", "box_format": "pixels"}, {"state": "a", "assets": {"": Image(PNG)}},
                     {"state": "a", "assets": {"x": PNG}}, {"state": Image(b""), "assets": None},
                     {"state": object()}):
            with self.assertRaises((ValueError, TypeError)):
                client.system_one(**args, questions={"answer": Noul()})
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
