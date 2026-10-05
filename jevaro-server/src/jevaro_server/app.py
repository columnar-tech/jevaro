"""POST /v1/systemone: JSON with state or a nonempty states array, or Arrow states in a form."""

import asyncio
import os
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated

import httpx2
import pyarrow as pa
from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typesafe_sdk import AsyncTypeSafeClient, Choice, JSONContent, Noul, RetryPolicy, Score
from typesafe_sdk.constants import DEFAULT_TIMEOUT

from .arrow import StreamSink, answer_batch, schema_for
from .form import FormError, media_type, read_parts
from .states import StatesError, read_states

MEDIA_TYPE = "application/vnd.apache.arrow.stream"
Question = Annotated[Choice | Score | Noul, Field(discriminator="type")]
# Clients that don't label a part send no Content-Type or application/octet-stream.
REQUEST_PART_TYPES = {None, "application/json"}
STATES_PART_TYPES = {None, "application/octet-stream", MEDIA_TYPE}
FORM_OPENAPI = {"requestBody": {"content": {"multipart/form-data": {
    "schema": {"type": "object", "required": ["request", "states"], "properties": {
        "request": {"type": "object", "description": "questions, model, and optional state_column"},
        "states": {"type": "string", "format": "binary", "description": "Arrow IPC stream; one state per row"},
    }},
    "encoding": {"request": {"contentType": "application/json"}, "states": {"contentType": MEDIA_TYPE}},
}}}}


def check_criteria(questions):
    for question in questions.values():
        if isinstance(question, Choice) and not 1 <= len(question.criteria) <= 255:
            raise ValueError("Choice requires 1 to 255 options")
        if isinstance(question, Score) and not 2 <= len(question.criteria) <= 10:
            raise ValueError("Score requires 2 to 10 levels")


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
        check_criteria(self.questions)
        return self


class FormRequest(BaseModel):
    """The JSON `request` part of a multipart request; the `states` part holds Arrow rows."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    questions: dict[str, Question] = Field(min_length=1)
    model: str = Field(default="jev-latest", min_length=1)
    state_column: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_request(self):
        check_criteria(self.questions)
        return self


def api_key_for(authorization):
    if authorization is None:
        api_key = os.environ.get("TYPESAFE_API_KEY")
        if not api_key:
            raise HTTPException(401, "Set TYPESAFE_API_KEY or send an Authorization bearer token")
        return api_key
    scheme, _, api_key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not api_key.strip():
        raise HTTPException(401, "Expected Authorization: Bearer <TYPESAFE_API_KEY>")
    return api_key.strip()


def succeeded(task):
    return task.done() and not task.cancelled() and task.exception() is None


async def ordered_responses(client, states, questions, model, concurrency):
    """Yield runs in input order: the next response plus any later ones already finished.

    A sliding window bounds running calls AND completed results awaiting their turn.
    """
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
            run = [await pending[0]]
            pending.popleft()
            # A failed call ends the run, so the rows before it are sent before the stream aborts.
            while pending and succeeded(pending[0]):
                run.append(pending.popleft().result())
            # Refill the window before the run is written, so upstream calls keep going meanwhile.
            for _ in run:
                submit()
            yield run
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def create_app(*, client_factory=AsyncTypeSafeClient, transport=None, concurrency=None,
               max_retries=None):
    if concurrency is None:
        concurrency = int(os.environ.get("JEVARO_CONCURRENCY", "256"))
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("JEVARO_CONCURRENCY must be a positive integer")
    if max_retries is None:
        max_retries = int(os.environ.get("JEVARO_MAX_RETRIES", "5"))
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("JEVARO_MAX_RETRIES must be a nonnegative integer")
    retry = RetryPolicy(max_retries=max_retries)

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

    app = FastAPI(title="Jevaro", version="0.3.0", lifespan=lifespan)

    def answer_schema(questions):
        try:
            return schema_for(questions)
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    def answer_stream(api_key, schema, questions, model, states):
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
                    http_client=app.state.http_client, retry=retry,
                )
                responses = ordered_responses(client, states, questions, model, concurrency)
                try:
                    async for run in responses:
                        writer.write_batch(answer_batch(schema, run))
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

    async def system_one_form(request):
        # Check the key before reading the body, so a rejected upload is never sent.
        api_key = api_key_for(request.headers.get("authorization"))
        try:
            parts = await read_parts(request, ("request", "states"))
        except FormError as error:
            raise HTTPException(422, str(error)) from error
        (request_type, request_data), (states_type, states_data) = parts["request"], parts["states"]
        if request_type not in REQUEST_PART_TYPES:
            raise HTTPException(422, f"The request part must be application/json, not {request_type}")
        if states_type not in STATES_PART_TYPES:
            raise HTTPException(422, f"The states part must be {MEDIA_TYPE}, not {states_type}")
        try:
            body = FormRequest.model_validate_json(request_data.decode())
        except UnicodeDecodeError as error:
            raise HTTPException(422, "The request part must be UTF-8 JSON") from error
        except ValidationError as error:
            raise RequestValidationError([
                {**detail, "loc": ("body", "request", *detail["loc"])}
                for detail in error.errors(include_url=False)
            ]) from error
        schema = answer_schema(body.questions)
        try:
            # Checking every row can take seconds for a large table; keep the event loop free.
            states = await asyncio.to_thread(read_states, states_data, body.state_column)
        except StatesError as error:
            raise HTTPException(422, str(error)) from error
        return answer_stream(api_key, schema, body.questions, body.model, states)

    class SystemOneRoute(APIRoute):
        """Multipart requests carry Arrow states; FastAPI handles every other body as JSON."""

        def get_route_handler(self):
            handle_json = super().get_route_handler()

            async def handle(request):
                if media_type(request.headers.get("content-type")) == "multipart/form-data":
                    return await system_one_form(request)
                return await handle_json(request)

            return handle

    router = APIRouter(route_class=SystemOneRoute)

    @router.post("/v1/systemone", response_class=StreamingResponse, openapi_extra=FORM_OPENAPI)
    async def system_one(
        body: Evaluation, authorization: Annotated[str | None, Header()] = None,
    ):
        api_key = api_key_for(authorization)
        states = body.states if body.states is not None else [body.state]
        return answer_stream(api_key, answer_schema(body.questions), body.questions, body.model, states)

    app.include_router(router)
    return app


app = create_app()
