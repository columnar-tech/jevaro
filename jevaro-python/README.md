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

`Choice`, `Score`, and `Noul` are re-exported from the official TypeSafe SDK.
Question dictionaries work too. See the bundled
[three-type example](https://github.com/columnar-tech/jevaro/blob/main/jevaro-python/example.py).

`system_one(state, questions, *, states=..., state_column=..., model=..., rows_per_call=..., timeout=..., extra_headers=...)`
returns an `ArrowReader`. Supply exactly one of `state` and `states`.

| Argument | Meaning |
| --- | --- |
| `state` | One string, object, or array |
| `states` | A nonempty list of states, or [tabular Arrow data](#arrow-states) |
| `state_column` | With Arrow states, the column whose values are the states |
| `questions` | A nonempty mapping of question IDs to definitions |
| `model` | Override the client's model |
| `rows_per_call` | States per upstream call, 1 to 256; above 1, [packs states](#pack-states-into-fewer-calls) |
| `timeout` | Override the client's I/O timeout, in seconds |
| `extra_headers` | Add or override headers for this request |

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

The schema uses Arrow extension metadata for Choice, Noul, and Score.
Choice selections are integer indices into shared labels, and Score legends
are stored once in the schema. Noul stores the probability of yes. See the
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
