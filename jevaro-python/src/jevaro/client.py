"""Small sync/async clients. HTTP bodies are read only as Arrow requests bytes."""

import asyncio
import io
import os
from collections.abc import Mapping

import httpx2
import pyarrow as pa
from typesafe_sdk import JSONContent, Question

from ._async_transport import AsyncTransport

MEDIA_TYPE = "application/vnd.apache.arrow.stream"
_MISSING = object()
_END = object()


class _HTTPInput(io.RawIOBase):
    def __init__(self, response):
        super().__init__()
        self.response = response
        self.chunks = iter(response.iter_bytes())
        self.pending = bytearray()

    def readable(self):
        return True

    def read(self, size=-1):
        if self.closed:
            raise ValueError("I/O operation on closed Arrow stream")
        while size < 0 or len(self.pending) < size:
            chunk = next(self.chunks, None)
            if chunk is None:
                break
            self.pending.extend(chunk)
        if size < 0:
            size = len(self.pending)
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def close(self):
        if not self.closed:
            try:
                self.response.close()
            finally:
                super().close()


class ArrowReader(pa.ipc.RecordBatchStreamReader):
    """A real PyArrow reader which also owns and closes its HTTP response."""

    def __init__(self, response, expected_rows, on_close):
        self._source = _HTTPInput(response)
        self._expected_rows = expected_rows
        self._rows = 0
        self._closed = False
        self._on_close = on_close
        try:
            super().__init__(self._source)
        except BaseException:
            self._source.close()
            raise

    def read_next_batch(self) -> pa.RecordBatch:
        try:
            batch = super().read_next_batch()
            self._rows += batch.num_rows
            if self._rows > self._expected_rows:
                raise OSError("Jevaro returned more rows than requested")
            return batch
        except StopIteration:
            self.close()
            if self._rows != self._expected_rows:
                raise OSError(
                    f"Incomplete Jevaro stream: expected {self._expected_rows} rows, got {self._rows}"
                ) from None
            raise
        except BaseException:
            self.close()
            raise

    def __iter__(self):
        return self

    def __next__(self):
        return self.read_next_batch()

    def read_all(self) -> pa.Table:
        return pa.Table.from_batches(self, schema=self.schema)

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                # Release HTTP before Arrow: an async caller may be cancelling
                # while a worker thread is blocked inside read_next_batch().
                self._source.close()
            finally:
                try:
                    super().close()
                finally:
                    self._on_close(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TypeSafeClient:
    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None,
        model: str | None = None, timeout: float = 60.0,
        headers: Mapping[str, str] | None = None,
        _transport: httpx2.BaseTransport | None = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
        self.base_url = (base_url or os.environ.get("TYPESAFE_BASE_URL")
                         or "http://127.0.0.1:8000").rstrip("/")
        self.model = model or os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
        self._http = httpx2.Client(timeout=timeout, headers=headers, transport=_transport)
        self._readers = set()

    def system_one(
        self, state: JSONContent = _MISSING,
        questions: Mapping[str, Question] | None = None, *,
        states: list[JSONContent] = _MISSING, model: str | None = None,
        timeout: float | None = None, extra_headers: Mapping[str, str] | None = None,
    ) -> ArrowReader:
        if (state is _MISSING) == (states is _MISSING):
            raise ValueError("Supply exactly one of state or states")
        if states is not _MISSING and (not isinstance(states, list) or not states):
            raise ValueError("states must be a nonempty list")
        if not questions:
            raise ValueError("questions must be a nonempty mapping")
        body = {
            "model": model or self.model,
            "questions": {
                name: q.model_dump(mode="json") if hasattr(q, "model_dump") else q
                for name, q in questions.items()
            },
        }
        body["state" if states is _MISSING else "states"] = state if states is _MISSING else states
        expected_rows = 1 if states is _MISSING else len(states)
        headers = {"Accept": MEDIA_TYPE}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(extra_headers or {})
        kwargs = {} if timeout is None else {"timeout": timeout}
        request = self._http.build_request(
            "POST", f"{self.base_url}/v1/systemone", json=body, headers=headers, **kwargs,
        )
        response = self._http.send(request, stream=True)
        try:
            if response.is_error:
                response.read()
                response.raise_for_status()
            if response.headers.get("content-type", "").split(";", 1)[0] != MEDIA_TYPE:
                raise ValueError("Expected an Arrow IPC stream from Jevaro")
            reader = ArrowReader(response, expected_rows, self._readers.discard)
        except BaseException:
            response.close()
            raise
        self._readers.add(reader)
        return reader

    def close(self):
        for reader in list(self._readers):
            reader.close()
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class AsyncArrowReader:
    """Async iteration over a PyArrow reader; blocking I/O runs in worker threads."""

    def __init__(self, reader: ArrowReader):
        self.reader = reader

    @property
    def schema(self):
        return self.reader.schema

    async def read_next_batch(self) -> pa.RecordBatch:
        def read():
            return next(self.reader, _END)

        try:
            batch = await asyncio.to_thread(read)
        except asyncio.CancelledError:
            await self.aclose()
            raise
        if batch is _END:
            raise StopAsyncIteration
        return batch

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.read_next_batch()

    async def read_all(self) -> pa.Table:
        return pa.Table.from_batches([batch async for batch in self], schema=self.schema)

    async def aclose(self):
        await asyncio.to_thread(self.reader.close)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


class AsyncTypeSafeClient:
    def __init__(self, **kwargs):
        self._transport = AsyncTransport()
        self._client = TypeSafeClient(**kwargs, _transport=self._transport)

    async def system_one(
        self, state: JSONContent = _MISSING,
        questions: Mapping[str, Question] | None = None, *,
        states: list[JSONContent] = _MISSING, model: str | None = None,
        timeout: float | None = None, extra_headers: Mapping[str, str] | None = None,
    ) -> AsyncArrowReader:
        self._transport.bind_loop()
        opening = asyncio.create_task(asyncio.to_thread(
            self._client.system_one, state, questions, states=states, model=model,
            timeout=timeout, extra_headers=extra_headers,
        ))
        try:
            return AsyncArrowReader(await asyncio.shield(opening))
        except asyncio.CancelledError:
            # A cancelled caller must not leave a subsequently opened HTTP stream alive.
            def cleanup(task):
                if not task.cancelled() and task.exception() is None:
                    asyncio.create_task(asyncio.to_thread(task.result().close))
            opening.add_done_callback(cleanup)
            raise

    async def aclose(self):
        self._transport.bind_loop()
        await asyncio.to_thread(self._client.close)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
