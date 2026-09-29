"""Let PyArrow's worker threads read an async HTTP connection owned by the loop.

Using async network I/O lets cancellation interrupt a blocked socket read;
closing a synchronous socket from a second thread can wait for its read timeout.
"""

import asyncio

import httpx2


class AsyncTransport(httpx2.BaseTransport):
    def __init__(self):
        self.client = httpx2.AsyncClient()
        self.loop = None

    def bind_loop(self):
        loop = asyncio.get_running_loop()
        if self.loop is not None and self.loop is not loop:
            raise RuntimeError("Use an AsyncTypeSafeClient in one event loop")
        self.loop = loop

    def run(self, coroutine):
        # Only called from the PyArrow/client worker threads, never the loop.
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def handle_request(self, request):
        response = self.run(self.client.send(request, stream=True))
        return httpx2.Response(
            response.status_code, headers=response.headers,
            stream=AsyncBody(response, self), extensions=response.extensions,
        )

    def close(self):
        self.run(self.client.aclose())


class AsyncBody(httpx2.SyncByteStream):
    def __init__(self, response, transport):
        self.response = response
        self.transport = transport

    def __iter__(self):
        chunks = self.response.aiter_raw()
        while (chunk := self.transport.run(anext(chunks, None))) is not None:
            yield chunk

    def close(self):
        self.transport.run(self.response.aclose())
