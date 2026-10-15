# Jevaro Python SDK

Send a batch of states and one shared set of questions to [Jev](https://docs.typesafe.ai/)
through the [Jevaro batching proxy](https://github.com/columnar-tech/jevaro/blob/main/jevaro-server/README.md)
and read the answers in input order as an [Apache Arrow](https://arrow.apache.org/)
stream using PyArrow.

Part of [Jevaro](https://github.com/columnar-tech/jevaro), an experiment in bulk
inference. The batching proxy handles concurrent API calls, retries, and
conversion from JSON to Arrow. This SDK returns a reader with one row per
state and one column per question.

Requires Python 3.11+ and a running Jevaro server. Start with the
[quickstart](https://github.com/columnar-tech/jevaro/blob/main/docs/quickstart.md).

## Install

In your Python project, add the SDK with [uv](https://docs.astral.sh/uv/concepts/projects/dependencies/):

```sh
uv add jevaro
```

## Read results

Save this as `example.py`. In your project, run `uv run example.py`; for a
standalone script, run `uv run --with jevaro example.py`.

```python
from jevaro import Noul, TypeSafeClient

with TypeSafeClient() as client:
    with client.system_one(
        states=["Please refund the shoes.", "Where is my parcel?"],
        questions={"refund": Noul(instructions="Is a refund being requested?")},
    ) as reader:
        print(reader.schema)
        for batch in reader:
            print(batch.to_pylist())
```

The reader is a `pyarrow.RecordBatchReader`. Its schema is available before
answers arrive. The first batch has zero rows; later batches have one or more
rows each.
Use `reader.read_all()` to collect a `pyarrow.Table`.

Keep the client open while reading. A context manager closes the HTTP response
if you stop early; full iteration closes it automatically.

## Questions and arguments

`Choice`, `Score`, `Noul`, `Box`, and `Image` are re-exported from the
official TypeSafe SDK. Question dictionaries work too. See the bundled
[three-type example](https://github.com/columnar-tech/jevaro/blob/main/jevaro-python/example.py).

`system_one(state, questions, *, states=..., state_column=..., model=..., rows_per_call=..., assets=..., box_format=..., timeout=..., extra_headers=...)`
returns an `ArrowReader`. Supply exactly one of `state` and `states`.

| Argument | Meaning |
| --- | --- |
| `state` | One string, object, array, or [image](#images-and-box-questions) |
| `states` | A nonempty list of states, or [tabular Arrow data](#arrow-states) |
| `state_column` | With Arrow states, the column whose values are the states |
| `questions` | A nonempty mapping of question IDs to definitions |
| `model` | Override the client's model |
| `rows_per_call` | States per upstream call, 1 to 256; above 1, [packs states](#pack-states-into-fewer-calls) |
| `assets` | [Images under names you choose](#name-images-in-text), to write inside text |
| `box_format` | `"typesafe"` (default) or `"geoarrow"`: how [Box answers](#box-answers) store their edges |
| `timeout` | Override the client's I/O timeout, in seconds |
| `extra_headers` | Add or override headers for this request, such as [danger-zone headers](#images-and-box-questions) |

An array in singular `state` is one evaluation. Use `states` to evaluate its
elements separately.

## Arrow states

`states` also accepts tabular Arrow data: a PyArrow table, record batch, or
reader, or any object with the Arrow PyCapsule stream interface
(`__arrow_c_stream__`), such as a Polars DataFrame. The SDK sends it to
Jevaro as an Arrow IPC stream. Each row becomes one state.

Without `state_column`, each state is an object of the row's columns, as
`tickets.to_pylist()` would produce. With `state_column`, each state is that
column's value, and the other columns are not sent:

```python
import pyarrow as pa
from jevaro import Noul, TypeSafeClient

tickets = pa.table({
    "id": [101, 102],
    "subject": ["Shoes", "Parcel"],
    "body": ["Please refund the shoes.", "Where is my parcel?"],
})

with TypeSafeClient() as client:
    # First state: {"id": 101, "subject": "Shoes", "body": "Please refund the shoes."}
    with client.system_one(
        states=tickets,
        questions={"refund": Noul(instructions="Does the customer's `body` ask for a refund?")},
    ) as reader:
        rows = reader.read_all()

    # First state: "Please refund the shoes."
    with client.system_one(
        states=tickets, state_column="body",
        questions={"refund": Noul(instructions="Is a refund being requested?")},
    ) as reader:
        bodies = reader.read_all()
```

To send some columns but not others, put them in a struct column and name it
as the state column. Here `id` stays in the table but out of the states:

```python
tickets = pa.table({
    "id": tickets["id"],
    "ticket": tickets.select(["subject", "body"]).to_struct_array(),
})
with TypeSafeClient() as client:
    # First state: {"subject": "Shoes", "body": "Please refund the shoes."}
    with client.system_one(
        states=tickets, state_column="ticket",
        questions={"refund": Noul(instructions="Does the ticket's `body` ask for a refund?")},
    ) as reader:
        answers = reader.read_all()
```

Answer rows follow input rows, so you can add the answers to the input table:

```python
results = tickets
for field in answers.schema:
    results = results.append_column(field, answers[field.name])
```

A Polars DataFrame works the same way:

```python
import polars as pl

messages = pl.DataFrame({"id": [101, 102], "body": ["Please refund the shoes.", "Where is my parcel?"]})
with TypeSafeClient() as client:
    with client.system_one(
        states=messages, state_column="body",
        questions={"refund": Noul(instructions="Is a refund being requested?")},
    ) as reader:
        answers = reader.read_all()
```

`pl.from_arrow(answers)` converts the answers to Polars. Polars warns that the
`jevaro.*` extension types are not registered; set
`POLARS_UNKNOWN_EXTENSION_TYPE_BEHAVIOR=load_as_storage` to load their storage
types without the warning.

A state column can also hold lists, maps, or `arrow.json` values. See the
[HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md#arrow-request)
for how Arrow types become JSON.

## Images and Box questions

States and questions can include images, and a `Box` question finds where
something is in one. TypeSafe calls these features
[dreamer-ready](https://docs.typesafe.ai/images): fit for prototypes, not yet
for production. Send the `TypeSafe-Danger-Zone-Images: true` header, which
Jevaro passes on to TypeSafe. `Box` needs Jev 1.14, so use `jev-preview`:

```python
from pathlib import Path
from jevaro import Box, Image, Noul, TypeSafeClient

IMAGES = {"TypeSafe-Danger-Zone-Images": "true"}
questions = {
    "mug": Box(instructions="the coffee mug in `photo`"),
    "mine": Noul(instructions={
        "question": "Is the mug in `photo` the one in `example`?",
        "example": Image(Path("my-mug.jpg")),
    }),
}
states = [
    {"id": 1, "photo": Image(Path("kitchen.jpg"))},
    {"id": 2, "photo": Image(Path("office.png"))},
]

with TypeSafeClient(model="jev-preview") as client:
    with client.system_one(states=states, questions=questions, extra_headers=IMAGES) as reader:
        for row in reader.read_all().to_pylist():
            print(row)
```

Each row has the box's edges, as fractions of the image's height and width,
and the Noul:

```python
{'mug': {'box': {'top': 0.25, 'bottom': 0.82, 'left': 0.44, 'right': 0.84}, 'confidence': 0.92}, 'mine': 0.87}
{'mug': {'box': {'top': 0.31, 'bottom': 0.64, 'left': 0.12, 'right': 0.35}, 'confidence': 0.88}, 'mine': 0.08}
```

The SDK writes each image as its reference, `str(image)`, such as
`<image:3fa85f64...>`, and uploads the file once, in its own part of the
request. Jevaro sends each upstream call the images that its state and the
questions refer to: here, one photo and `my-mug.jpg`. An `Image` can come from
a path, bytes, `Image.from_base64()`, or `Image.from_pil()`.

To send the header with every request, create the client with
`headers={"TypeSafe-Danger-Zone-Images": "true"}`. Jevaro passes on every
`TypeSafe-Danger-Zone-*` header, including `TypeSafe-Danger-Zone-Long-Context`.
`rows_per_call` can't be used with images or Box questions.

### Images in Arrow states

In [Arrow states](#arrow-states), each binary value is an image. Jevaro writes
a reference in its place and sends the image with that row's call:

```python
photos = pa.table({
    "id": [1, 2],
    "photo": [Path("kitchen.jpg").read_bytes(), Path("office.png").read_bytes()],
})

with TypeSafeClient(model="jev-preview") as client:
    # First state: {"id": 1, "photo": "<image:5d1c2a7e0b9f4c3d8e6a000000000000>"}
    with client.system_one(
        states=photos, questions={"mug": Box(instructions="the coffee mug in `photo`")},
        extra_headers=IMAGES,
    ) as reader:
        boxes = reader.read_all()
```

With `state_column="photo"`, each state is the image alone; write instructions
such as `"the coffee mug"`. Polars `Binary` columns work the same way.

### Name images in text

To write an image inside text, pass it in `assets` under a name of your
choice, and write the name where the image belongs:

```python
with client.system_one(
    states=[{"id": 1, "photo": Image(Path("kitchen.jpg"))}],
    questions={"mine": Noul(instructions="Is the mug in `photo` this one: <my-mug>?")},
    assets={"<my-mug>": Image(Path("my-mug.jpg"))},
    extra_headers=IMAGES,
) as reader:
    print(reader.read_all()["mine"].to_pylist())  # [0.87]
```

Use names that can't appear in your text by accident. Every name must appear
in a state, instructions, or criteria.

### Box answers

By default, a Box column keeps TypeSafe's edge names. With
`box_format="geoarrow"`, its `box` field is a
[GeoArrow](https://geoarrow.org/) box:

```python
with client.system_one(states=photos, questions={"mug": Box(instructions="the coffee mug in `photo`")},
                       box_format="geoarrow", extra_headers=IMAGES) as reader:
    print(reader.read_all()["mug"].to_pylist()[0])
# {'box': {'xmin': 0.44, 'ymin': 0.25, 'xmax': 0.84, 'ymax': 0.82}, 'confidence': 0.92}
```

`confidence` is `None` when TypeSafe doesn't report one. See the
[Arrow schema](https://github.com/columnar-tech/jevaro/blob/main/docs/arrow-schema.md#box)
for both layouts.

TypeSafe checks each image: PNG or JPEG, 256 to 1,568 pixels on each side, and
its [other limits](https://docs.typesafe.ai/images#limits). An image that it
rejects ends the stream at that row, so
[prepare images](https://docs.typesafe.ai/images#preparing-images) first.
See the [HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md#images-and-box-questions)
for which images each call sends and the checks Jevaro makes first.

## Pack states into fewer calls

`rows_per_call` puts up to that many states in each upstream call:

```python
with client.system_one(states=messages, questions=questions, rows_per_call=64) as reader:
    answers = reader.read_all()
```

The answers have the same schema and row order as without it.

In a live test of short messages with three questions, this raised throughput
from 1,040 to 2,000 states per second and used 28% fewer input tokens.

There are two tradeoffs:

- **Answers shift slightly, and consistently.** About 1.5% of Choice answers
  differed from one state per call.
- **Long states with many questions cost more.** Each question carries its
  own copy of the state.

See the [HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md#pack-states-into-fewer-calls)
for how it works and which questions it rejects.

## Client configuration

`TypeSafeClient` and `AsyncTypeSafeClient` accept these keyword arguments:

| Option | Default |
| --- | --- |
| `api_key` | `TYPESAFE_API_KEY`; otherwise the server may supply its key |
| `base_url` | `TYPESAFE_BASE_URL`, then `http://127.0.0.1:8000` |
| `model` | `TYPESAFE_DEFAULT_MODEL`, then `jev-latest` |
| `timeout` | 60 seconds per HTTP I/O operation |
| `headers` | No extra headers |

The timeout is not a deadline for the whole batch. Call `client.close()` when
not using a context manager; use `await client.aclose()` for the async client.

## Async

```python
import asyncio
from jevaro import AsyncTypeSafeClient, Noul

async def main():
    async with AsyncTypeSafeClient() as client:
        async with await client.system_one(
            states=["Please refund the shoes.", "Where is my parcel?"],
            questions={"refund": Noul(instructions="Is a refund being requested?")},
        ) as reader:
            async for batch in reader:
                print(batch.to_pylist())

asyncio.run(main())
```

`AsyncArrowReader` has `schema`, async iteration, `await read_next_batch()`,
`await read_all()`, and `await aclose()`. Read methods return PyArrow batches or
a table. `read_next_batch()` raises `StopAsyncIteration` at the end.

Use the async interface for normal reads. Its `reader` property exposes the
underlying PyArrow reader; direct reads must run through `asyncio.to_thread`.
Consume a reader from one task at a time.

## Metadata and files

The schema uses Arrow extension metadata for Choice, Noul, Score, and Box.
Choice selections are integer indices into shared labels, and Score legends
are stored once in the schema. Noul stores the probability of yes, and Box
stores four edges and a confidence. See the
[Arrow schema](https://github.com/columnar-tech/jevaro/blob/main/docs/arrow-schema.md)
for decoding rules. `batch.to_pylist()` exposes these storage values.

To save a stream, open a writer with `reader.schema` and pass each batch to
`writer.write_batch(batch)`. The bundled example does this:

```sh
uv run --with jevaro jevaro-python/example.py --output jevaro-results.arrows
uv run --with pyarrow scripts/read_results.py jevaro-results.arrows
```

`read_results.py` reconstructs full answer objects using only the saved schema
and PyArrow. Run these commands from the repository root.

## Errors and compatibility

Invalid argument combinations raise `ValueError`. HTTP errors before streaming
raise `httpx2.HTTPStatusError`. Network or Arrow errors can occur during reading.
Unexpected row counts raise `OSError`.

This SDK implements System One evaluation with Arrow results. Model listing,
custom JSON response models, usage objects, and the official SDK's error
classes are outside its API. Per-state retries happen at the batching proxy;
the SDK does not retry an entire batch.

See [Contributing](https://github.com/columnar-tech/jevaro/blob/main/CONTRIBUTING.md)
for tests.

[Apache-2.0](https://github.com/columnar-tech/jevaro/blob/main/LICENSE).
Copyright 2026 Columnar Technologies Inc.
