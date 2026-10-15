"""POST /v1/systemone: JSON with state or a nonempty states array, or a multipart form with images or Arrow states."""

import asyncio
import os
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Literal

import httpx2
import pyarrow as pa
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typesafe_sdk import AsyncTypeSafeClient, Box, Choice, JSONContent, Noul, RetryPolicy, Score
from typesafe_sdk.constants import DEFAULT_TIMEOUT

from .arrow import StreamSink, answer_batch, schema_for
from .form import FormError, media_type, read_parts
from .images import IMAGES_HEADER, Asset, ImageError, Images, forwarded_headers, images_enabled, load_assets
from .packing import MAX_ROWS_PER_CALL, PackingError, packed_responses, state_checker, templates_for
from .states import StatesError, read_states
from .throttle import ThrottledTransport

MEDIA_TYPE = "application/vnd.apache.arrow.stream"
Question = Annotated[Choice | Score | Noul | Box, Field(discriminator="type")]
# Clients that don't label a part send no Content-Type or application/octet-stream.
REQUEST_PART_TYPES = {None, "application/json"}
STATES_PART_TYPES = {None, "application/octet-stream", MEDIA_TYPE}
FORM_OPENAPI = {"requestBody": {"content": {"multipart/form-data": {
    "schema": {"type": "object", "required": ["request"], "properties": {
        "request": {"type": "object", "description": "the JSON request fields; state or states unless a states "
                                                     "part is sent, and assets naming the image parts"},
        "states": {"type": "string", "format": "binary", "description": "Arrow IPC stream; one state per row"},
    }, "additionalProperties": {"type": "string", "format": "binary", "description": "an image file named by assets"}},
    "encoding": {"request": {"contentType": "application/json"}, "states": {"contentType": MEDIA_TYPE}},
}}}}


def check_criteria(questions):
    for question in questions.values():
        if isinstance(question, Choice) and not 1 <= len(question.criteria) <= 255:
            raise ValueError("Choice requires 1 to 255 options")
        if isinstance(question, Score) and not 2 <= len(question.criteria) <= 10:
            raise ValueError("Score requires 2 to 10 levels")


RowsPerCall = Annotated[int, Field(ge=1, le=MAX_ROWS_PER_CALL, strict=True)]


class Fields(BaseModel):
    """Request fields shared by JSON requests and the JSON `request` part of a multipart request."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    questions: dict[str, Question] = Field(min_length=1)
    model: str = Field(default="jev-latest", min_length=1)
    rows_per_call: RowsPerCall = 1
    assets: dict[str, Asset] | None = None
    box_format: Literal["typesafe", "geoarrow"] = "typesafe"

    @model_validator(mode="after")
    def validate_fields(self):
        check_criteria(self.questions)
        return self


class Evaluation(Fields):
    state: JSONContent | None = None
    states: list[JSONContent] | None = None

    @model_validator(mode="after")
    def validate_states(self):
        singular = "state" in self.model_fields_set
        plural = "states" in self.model_fields_set
        if singular == plural:
            raise ValueError("Supply exactly one of state or states")
        if singular and self.state is None:
            raise ValueError("state must be a string, object, or array")
        if plural and not self.states:
            raise ValueError("states must be a nonempty array")
        return self


class FormRequest(Fields):
    """The JSON `request` part; its states, or a `states` part of Arrow rows."""

    state: JSONContent | None = None
    states: list[JSONContent] | None = None
    state_column: str | None = Field(default=None, min_length=1)


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


async def ordered_responses(client, calls, questions, model, concurrency):
    """Yield runs in input order: the next response plus any later ones already finished.

    calls yields (state, assets or None). A sliding window bounds running calls AND
    completed results awaiting their turn.
    """
    remaining = iter(calls)
    pending = deque()

    def submit():
        try:
            state, assets = next(remaining)
        except StopIteration:
            return
        extra = {} if assets is None else {"assets": assets}
        pending.append(asyncio.create_task(
            client.system_one(state=state, questions=questions, model=model, **extra)
        ))

    try:
        for _ in range(concurrency):
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


class Spread:
    """Send each call over the upstream connection with the fewest calls in flight."""

    def __init__(self, clients, in_flight):
        self.clients = clients
        self.in_flight = in_flight

    async def system_one(self, **kwargs):
        index = min(range(len(self.clients)), key=self.in_flight.__getitem__)
        self.in_flight[index] += 1
        try:
            return await self.clients[index].system_one(**kwargs)
        finally:
            self.in_flight[index] -= 1


class Plan:
    """A checked request, ready to stream: its schema, questions, states, and images."""

    def __init__(self, body, headers, parts=None):
        self.body = body
        self.headers = forwarded_headers(headers)
        self.images_on = images_enabled(headers)
        self.schema = self.answer_schema()
        try:
            self.images = Images(load_assets(body.assets or {}, parts), body.questions)
        except ImageError as error:
            raise HTTPException(422, str(error)) from error
        self.boxes = any(isinstance(q, Box) for q in body.questions.values())
        self.require_images(bool(self.images.images) or self.boxes)
        self.templates = self.packing_templates()
        self.states = None
        # A function that yields (state, images from its Arrow binary values) for each row.
        self.rows = None
        self.generated = False

    def answer_schema(self):
        try:
            return schema_for(self.body.questions, self.body.box_format)
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    def packing_templates(self):
        """Questions prepared for packing, or None for one row per call."""
        if self.body.rows_per_call == 1:
            return None
        try:
            return templates_for(self.body.questions)
        except PackingError as error:
            raise HTTPException(422, str(error)) from error

    def require_images(self, needed):
        if not needed:
            return
        if not self.images_on:
            raise HTTPException(422, f"Images and Box questions are experimental at TypeSafe. Send the header "
                                     f"{IMAGES_HEADER}: true, and Jevaro sends it to TypeSafe with each call.")
        if self.body.rows_per_call > 1:
            raise HTTPException(422, "rows_per_call above 1 can't be used with images or Box questions: "
                                     "packing would send each row's images again with every question.")

    def check_row(self):
        """A function that checks one state before streaming, or None when no check is needed."""
        packing = self.templates and state_checker(self.templates)
        images = self.images if self.images.images else None
        if not (packing or images):
            return None

        def check(state, generated, row):
            if packing:
                packing(state, row)
            if images:
                images.check(state, generated, row)

        return check

    def check_json_states(self, states):
        if check := self.check_row():
            try:
                for row, state in enumerate(states):
                    check(state, {}, row)
            except (PackingError, ImageError) as error:
                raise HTTPException(422, str(error)) from error
        self.finish(states, lambda: ((state, {}) for state in states))

    async def read_arrow_states(self, data, state_column):
        try:
            # Checking every row can take seconds for a large table; keep the event loop free.
            states = await asyncio.to_thread(read_states, data, state_column, self.check_row())
        except (StatesError, PackingError, ImageError) as error:
            raise HTTPException(422, str(error)) from error
        self.generated = states.images > 0
        self.require_images(self.generated)
        self.finish(states, states.rows)

    def finish(self, states, rows):
        try:
            self.images.check_used()
        except ImageError as error:
            raise HTTPException(422, str(error)) from error
        self.states = states
        self.rows = rows

    def calls(self):
        """(state, assets or None) for each row, in order, when every row is its own call."""
        rows = self.rows()
        if not self.sends_images:
            return ((state, None) for state, _ in rows)
        return ((state, self.images.for_state(state, generated)) for state, generated in rows)

    @property
    def sends_images(self):
        return bool(self.images.images or self.generated)


def create_app(*, client_factory=AsyncTypeSafeClient, transport=None, transport_factory=None,
               concurrency=None, max_retries=None, connections=None):
    if concurrency is None:
        concurrency = int(os.environ.get("JEVARO_CONCURRENCY", "512"))
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("JEVARO_CONCURRENCY must be a positive integer")
    if max_retries is None:
        max_retries = int(os.environ.get("JEVARO_MAX_RETRIES", "5"))
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("JEVARO_MAX_RETRIES must be a nonnegative integer")
    if connections is None:
        connections = int(os.environ.get("JEVARO_UPSTREAM_CONNECTIONS", "4"))
    if isinstance(connections, bool) or not isinstance(connections, int) or connections < 1:
        raise ValueError("JEVARO_UPSTREAM_CONNECTIONS must be a positive integer")
    retry = RetryPolicy(max_retries=max_retries)

    @asynccontextmanager
    async def lifespan(app):
        # Shared pools for every incoming request keep upstream connections warm,
        # and HTTP/2 multiplexes parallel calls over each connection. TypeSafe
        # allows 100 calls at a time per connection, and httpx2 opens only one
        # per pool, so each extra pool adds a connection. Every attempt waits for
        # its API key's adaptive limit, which backs off on 429 and 529. The SDK
        # inherits this timeout, so match its own default.
        throttles = {}
        async with AsyncExitStack() as stack:
            app.state.http_clients = []
            for _ in range(connections):
                inner = (transport_factory() if transport_factory
                         else transport or httpx2.AsyncHTTPTransport(http2=True))
                app.state.http_clients.append(await stack.enter_async_context(httpx2.AsyncClient(
                    http2=True, timeout=DEFAULT_TIMEOUT,
                    transport=ThrottledTransport(inner, throttles, ceiling=100 * connections),
                )))
            app.state.in_flight = [0] * connections
            app.state.throttles = throttles
            yield

    app = FastAPI(title="Jevaro", version="0.4.0", lifespan=lifespan)

    def answer_stream(api_key, plan):
        schema, body = plan.schema, plan.body
        # Image files are already compressed; don't spend time compressing their bodies again.
        options = {"compression": False} if plan.sends_images else {}

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
                # This request's key and danger-zone headers over the shared pools. Never
                # close these clients: the SDK would close the shared pool along with them.
                clients = [client_factory(
                    api_key=api_key,
                    base_url=os.environ.get("TYPESAFE_UPSTREAM_URL", "https://api.typesafe.ai"),
                    http_client=http_client, retry=retry, headers=plan.headers, **options,
                ) for http_client in app.state.http_clients]
                client = clients[0] if len(clients) == 1 else Spread(clients, app.state.in_flight)
                if plan.templates is None:
                    responses = ordered_responses(client, plan.calls(), body.questions, body.model, concurrency)
                else:
                    responses = packed_responses(client, plan.states, plan.templates, body.model, concurrency,
                                                 body.rows_per_call)
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
            "X-Jevaro-Row-Count": str(len(plan.states)),
        })

    async def system_one_form(request):
        # Check the key before reading the body, so a rejected upload is never sent.
        api_key = api_key_for(request.headers.get("authorization"))
        try:
            parts = await read_parts(request)
        except FormError as error:
            raise HTTPException(422, str(error)) from error
        if "request" not in parts:
            raise HTTPException(422, "Missing part 'request'")
        request_type, request_data = parts.pop("request")
        arrow = parts.pop("states", None)
        if request_type not in REQUEST_PART_TYPES:
            raise HTTPException(422, f"The request part must be application/json, not {request_type}")
        if arrow and arrow[0] not in STATES_PART_TYPES:
            raise HTTPException(422, f"The states part must be {MEDIA_TYPE}, not {arrow[0]}")
        try:
            body = FormRequest.model_validate_json(request_data.decode())
        except UnicodeDecodeError as error:
            raise HTTPException(422, "The request part must be UTF-8 JSON") from error
        except ValidationError as error:
            raise RequestValidationError([
                {**detail, "loc": ("body", "request", *detail["loc"])}
                for detail in error.errors(include_url=False)
            ]) from error
        json_states = {"state", "states"} & body.model_fields_set
        if arrow and json_states:
            raise HTTPException(422, f"Send states in the states part or as {min(json_states)} in the request "
                                     "part, not both")
        if not arrow:
            if len(json_states) != 1:
                raise HTTPException(422, "Missing part 'states': send Arrow states in a states part, or state "
                                         "or states in the request part")
            if body.state_column is not None:
                raise HTTPException(422, "state_column requires Arrow states in a states part")
            if json_states == {"state"} and body.state is None:
                raise HTTPException(422, "state must be a string, object, or array")
            if json_states == {"states"} and not body.states:
                raise HTTPException(422, "states must be a nonempty array")
        plan = Plan(body, request.headers, parts)
        if arrow:
            await plan.read_arrow_states(arrow[1], body.state_column)
        else:
            plan.check_json_states(body.states if body.states is not None else [body.state])
        return answer_stream(api_key, plan)

    class SystemOneRoute(APIRoute):
        """Multipart requests carry images or Arrow states; FastAPI handles every other body as JSON."""

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
        request: Request, body: Evaluation, authorization: Annotated[str | None, Header()] = None,
    ):
        api_key = api_key_for(authorization)
        plan = Plan(body, request.headers)
        plan.check_json_states(body.states if body.states is not None else [body.state])
        return answer_stream(api_key, plan)

    app.include_router(router)
    return app


app = create_app()
