import io
import unittest

import httpx2
import pyarrow as pa
from jevaro import Noul, TypeSafeClient


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


class ClientTests(unittest.TestCase):
    def client(self, values=(0.12345678901234567,), status=200, content_type="application/vnd.apache.arrow.stream"):
        client = TypeSafeClient(api_key="test-key")
        client._http.close()
        stream = Chunks(ipc(values))
        client._http = httpx2.Client(transport=httpx2.MockTransport(lambda request: httpx2.Response(
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

    def test_invalid_arguments(self):
        client, _ = self.client()
        for args in ({}, {"states": []}, {"states": "no"}, {"state": "a", "states": ["b"]}):
            with self.assertRaises(ValueError):
                client.system_one(**args, questions={"answer": Noul()})


if __name__ == "__main__":
    unittest.main()
