"""POST /v1/systemone: TypeSafe requests with state or a nonempty states array."""

import asyncio
import os
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated

import httpx2
import pyarrow as pa
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typesafe_sdk import AsyncTypeSafeClient, Choice, JSONContent, Noul, Score
from typesafe_sdk.constants import DEFAULT_TIMEOUT

from .arrow import StreamSink, answer_batch, schema_for

MEDIA_TYPE = "application/vnd.apache.arrow.stream"
Question = Annotated[Choice | Score | Noul, Field(discriminator="type")]


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    state: JSONContent | None = None
    states: list[JSONContent] | None = None
    questions: dict[str, Question] = Field(min_length=1)
    model: str = Field(default="jev-latest", min_length=1)

    @model_validator(mode="after")
    def validate_request(self):
        singular = "state" in self.model_fields_set
        plural = "states" in self.model_fields_set
        if singular == plural:
            raise ValueError("Supply exactly one of state or states")
        if singular and self.state is None:
            raise ValueError("state must be a string, object, or array")
        if plural and not self.states:
            raise ValueError("states must be a nonempty array")
        for question in self.questions.values():
            if isinstance(question, Choice) and not 1 <= len(question.criteria) <= 255:
                raise ValueError("Choice requires 1 to 255 options")
            if isinstance(question, Score) and not 2 <= len(question.criteria) <= 10:
                raise ValueError("Score requires 2 to 10 levels")
        return self


async def ordered_responses(client, states, questions, model, concurrency):
    """A sliding window bounds running calls AND completed results awaiting their turn."""
    remaining = iter(states)
    pending = deque()

    def submit():
        try:
            state = next(remaining)
        except StopIteration:
            return
        pending.append(asyncio.create_task(
            client.system_one(state=state, questions=questions, model=model)
        ))

    try:
        for _ in range(min(concurrency, len(states))):
            submit()
        while pending:
            # Keep the task in the deque while awaiting it, so cancellation catches it too.
            response = await pending[0]
            pending.popleft()
            yield response
            submit()
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def create_app(*, client_factory=AsyncTypeSafeClient, transport=None, concurrency=None):
    if concurrency is None:
        concurrency = int(os.environ.get("JEVARO_CONCURRENCY", "8"))
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("JEVARO_CONCURRENCY must be a positive integer")

    @asynccontextmanager
    async def lifespan(app):
        # One pool for every incoming request keeps upstream connections warm, and
        # HTTP/2 multiplexes a batch's parallel calls over one connection. The SDK
        # inherits this timeout, so match its own default.
        async with httpx2.AsyncClient(
            http2=True, timeout=DEFAULT_TIMEOUT, transport=transport,
        ) as http_client:
            app.state.http_client = http_client
            yield

    app = FastAPI(title="Jevaro", version="0.1.0", lifespan=lifespan)

    @app.post("/v1/systemone", response_class=StreamingResponse)
    async def system_one(
        body: Evaluation, authorization: Annotated[str | None, Header()] = None,
    ):
        if authorization is not None:
            scheme, _, api_key = authorization.partition(" ")
            if scheme.lower() != "bearer" or not api_key.strip():
                raise HTTPException(401, "Expected Authorization: Bearer <TYPESAFE_API_KEY>")
            api_key = api_key.strip()
        else:
            api_key = os.environ.get("TYPESAFE_API_KEY")
        if not api_key:
            raise HTTPException(401, "Set TYPESAFE_API_KEY or send an Authorization bearer token")

        states = body.states if body.states is not None else [body.state]
        try:
            schema = schema_for(body.questions)
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

        async def stream():
            sink = StreamSink()
            writer = pa.ipc.new_stream(sink, schema)
            try:
                # Arrow emits the schema on the first write. Flush a zero-row batch
                # before constructing the upstream client or starting any API calls.
                writer.write_batch(pa.RecordBatch.from_arrays(
                    [field.type.array([]) for field in schema], schema=schema,
                ))
                yield sink.drain()
                # This request's key over the shared pool. Never close this client:
                # the SDK would close the shared pool along with it.
                client = client_factory(
                    api_key=api_key,
                    base_url=os.environ.get("TYPESAFE_UPSTREAM_URL", "https://api.typesafe.ai"),
                    http_client=app.state.http_client,
                )
                responses = ordered_responses(
                    client, states, body.questions, body.model, concurrency,
                )
                try:
                    async for response in responses:
                        writer.write_batch(answer_batch(schema, response))
                        yield sink.drain()
                finally:
                    await responses.aclose()
                writer.close()
                yield sink.drain()  # Normal completion alone sends the Arrow EOS marker.
            finally:
                # On failure/disconnect, discard any buffered EOS; the HTTP stream aborts.
                writer.close()
                sink.close()

        return StreamingResponse(stream(), media_type=MEDIA_TYPE, headers={
            "X-Accel-Buffering": "no",
            "X-Jevaro-Row-Count": str(len(states)),
        })

    return app


app = create_app()
