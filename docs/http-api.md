# HTTP API

Jevaro accepts TypeSafe System One questions and returns an Arrow IPC stream.
Send the states as JSON, or as an [Arrow IPC stream](#arrow-request).
The local default endpoint is `POST http://127.0.0.1:8000/v1/systemone`.

## Request

Send JSON with `Content-Type: application/json`:

```json
{
  "model": "jev-latest",
  "states": ["Please refund the shoes.", "Where is my parcel?"],
  "questions": {
    "refund": {
      "type": "noul",
      "instructions": "Is a refund being requested?"
    }
  }
}
```

| Field | Rule |
| --- | --- |
| `state` | One string, object, or array |
| `states` | A nonempty array of states; no configured element-count limit |
| `questions` | A nonempty map of string IDs to question definitions |
| `model` | Nonempty string; defaults to `jev-latest` |
| `rows_per_call` | Integer from 1 to 256; defaults to 1. Above 1, [packs states](#pack-states-into-fewer-calls) into fewer upstream calls |

Supply exactly one of `state` and `states`. `state: ["a", "b"]` is one
evaluation; `states: ["a", "b"]` is two. An individual state cannot be a
number, boolean, or null. Unknown top-level fields are rejected.

Questions use the [TypeSafe definitions](https://docs.typesafe.ai/api):

| `type` | `criteria` |
| --- | --- |
| `choice` | Map of 1–255 labels to descriptions; a description may be null |
| `score` | Ordered array of 2–10 level descriptions |
| `noul` | Optional object describing `true` and `false` outcomes |

Instructions and descriptions can use strings, objects, or arrays. The proxy
accepts omitted or null instructions, as the SDK question helpers do.
Every state uses the same questions and model.

## Arrow request

To send states as Arrow, POST `multipart/form-data` with two parts:

| Part | Content-Type | Contents |
| --- | --- | --- |
| `request` | `application/json` | `questions`, `model`, and optional `state_column` and `rows_per_call` |
| `states` | `application/vnd.apache.arrow.stream` | An Arrow IPC stream with one row per state |

The `request` part takes the same `questions` and `model` as a JSON request,
and rejects `state` and `states`. A part may omit its Content-Type; `states`
may also use `application/octet-stream`. Any other part is rejected. The
response is the same Arrow stream as for a JSON request.

### Rows become states

Each row is one state. These examples use a table of two support tickets:

| `id` | `subject` | `body` |
| --- | --- | --- |
| 101 | Shoes | Please refund the shoes. |
| 102 | Parcel | Where is my parcel? |

Without `state_column`, each state is an object of the row's columns, as
PyArrow's `table.to_pylist()` would produce. The two states are:

```json
{"id": 101, "subject": "Shoes", "body": "Please refund the shoes."}
{"id": 102, "subject": "Parcel", "body": "Where is my parcel?"}
```

Instructions can refer to a column by name, as in
``"Does the customer's `body` ask for a refund?"``.

With `"state_column": "body"`, that column's values are the states. The other
columns are not sent:

```json
"Please refund the shoes."
"Where is my parcel?"
```

To send some columns but not others, put them in a struct column and name it.
If `ticket` is a struct column with `subject` and `body` fields,
`"state_column": "ticket"` sends these states, and `id` stays out of them:

```json
{"subject": "Shoes", "body": "Please refund the shoes."}
{"subject": "Parcel", "body": "Where is my parcel?"}
```

A state column can hold strings, structs, lists, maps, or `arrow.json`
values. Each `arrow.json` value is parsed, so states can differ in shape from
row to row. A state column cannot contain nulls. Answer rows follow input
rows, so you can join answers to the input by position.

### Send a table with curl

Write the table as an Arrow IPC stream. With PyArrow:

```python
import pyarrow as pa

tickets = pa.table({
    "id": [101, 102],
    "subject": ["Shoes", "Parcel"],
    "body": ["Please refund the shoes.", "Where is my parcel?"],
})
with pa.ipc.new_stream("tickets.arrows", tickets.schema) as writer:
    writer.write_table(tickets)
```

Save the request fields as `request.json`. This one sends each `body` as a
state:

```json
{
  "questions": {
    "refund": {"type": "noul", "instructions": "Is a refund being requested?"}
  },
  "state_column": "body"
}
```

Send both parts:

```sh
curl --fail-with-body --no-buffer http://127.0.0.1:8000/v1/systemone \
  -F 'request=<request.json;type=application/json' \
  -F 'states=@tickets.arrows;type=application/vnd.apache.arrow.stream' \
  --output http-results.arrows
```

To send whole rows instead, remove `state_column` from `request.json` and
refer to the columns in the instructions.

### Types

| Arrow type | JSON value |
| --- | --- |
| String, large string, string view | String |
| Boolean, integer, float | Boolean or number; NaN and infinities are rejected |
| Date, time, timestamp, decimal | String from Arrow's cast, such as `2024-01-02 03:04:05.123456` or `12.50` |
| Struct | Object |
| List, large list, fixed-size list | Array |
| Map with string keys | Object; duplicate keys are rejected |
| Dictionary | Its decoded value |
| `arrow.json` extension | The parsed JSON value |
| Null | `null` |

A null inside a row becomes `null`. Other types, including binary, list views,
durations, and unions, are rejected. Send the IPC stream format; the IPC file
format is rejected.

### Validation and memory

The server checks the API key before reading the body. It then reads the whole
upload and converts every row once, so a bad row causes a 422 before streaming
starts. The upload stays in memory as Arrow data. Rows are converted again,
1,024 at a time, as upstream calls start.

## Pack states into fewer calls

By default, Jevaro makes one upstream call per state. With `rows_per_call`
above 1, each call evaluates up to that many states. Each state goes into its
own copy of every question's instructions, and the call's `state` is empty.
TypeSafe evaluates the questions in a call independently, so states can't
affect each other's answers.

This request:

```json
{
  "states": ["Please refund the shoes.", "Where is my parcel?"],
  "questions": {"refund": {"type": "noul", "instructions": "Is a refund being requested?"}},
  "rows_per_call": 2
}
```

makes this single upstream call instead of two:

```json
{
  "model": "jev-latest",
  "state": "",
  "questions": {
    "0.0": {"type": "noul", "instructions": {"state": "Please refund the shoes.", "question": "Is a refund being requested?"}},
    "1.0": {"type": "noul", "instructions": {"state": "Where is my parcel?", "question": "Is a refund being requested?"}}
  }
}
```

The response is the same Arrow stream: one row per state, in input order.

- **Object instructions keep their fields.** `state` is added beside them.
- **Paths are rewritten.** A backticked path that names part of the state is
  rewritten in instructions and criteria. With the state
  `{"ticket": {"body": "Please refund the shoes."}}`, the instruction
  ``"Does `ticket.body` ask for a refund?"`` becomes
  ``"Does `state.ticket.body` ask for a refund?"``. Choice options and the
  schema's Score legends keep their original text.
- **A call can hold fewer states than `rows_per_call`.** Each call is kept to
  about 48,000 estimated tokens. If TypeSafe still rejects one as too large,
  Jevaro splits it in two.

**Throughput.** One live test used three questions about short support
messages (about 180 characters each):

| Setting | States per second |
| --- | --- |
| One state per call | 1,040 |
| `rows_per_call: 64`, 100,000 states | 2,000 |
| `rows_per_call: 64`, 10,000 states | 9,600 |

The sustained rate is set by TypeSafe's input token rate limit for the
account. A short job runs faster while TypeSafe's burst allowance lasts. When
TypeSafe returns 429, Jevaro lowers the number of calls in flight.

**Tradeoffs.**

- **Answers change slightly.** Compared with one state per call, the
  differences were 2 to 4 times the variation between two unpacked runs.
  - About 1.5% of Choice answers differed.
  - A Score with levels 0 to 2 averaged about 0.03 lower.

  The changes are consistent: packed answers don't depend on the batch size,
  the other states in a call, or a state's position. Don't mix packed and
  unpacked answers, and recheck any thresholds tuned on unpacked answers.
- **Cost depends on state length and question count.** Every question carries
  a copy of its state.
  - Short states with few questions use fewer input tokens, because calls
    share a fixed overhead of about 260 tokens: 28% fewer in the test above.
  - Long states with many questions use more: 1.8 times as many for structured
    tickets with 6 questions. Under a token rate limit, they're also slower
    than one state per call once the burst allowance is spent.

**Conflicts.** Before any upstream call, Jevaro rejects two kinds of request
with 422:

- **A `state` field in instructions.** For example, a question's instructions
  are `{"state": "CA", "question": "Is this on the west coast?"}`. Rename the
  field.
- **A name shared by the instructions and the state.** A question refers in
  backticks to a field of its instructions object, and a state has a
  top-level field with the same name. Take these instructions with the state
  `{"policy": "60-day returns"}`:

  ```json
  {"policy": "30-day returns", "question": "Is the request covered by `policy`?"}
  ```

  Once the state moves into the instructions, `` `policy` `` would name only
  the instructions field. Rename that field.

The error names the question and, for a shared name, the row.

## Credentials

Send `Authorization: Bearer <TYPESAFE_API_KEY>`, or let the server use its
`TYPESAFE_API_KEY` environment variable. A supplied header takes precedence.
The server forwards the selected key to TypeSafe.

See [server configuration](../jevaro-server/README.md#configuration) for
shared-server access and environment settings.

## Response

A valid request starts a response with:

| Header | Value |
| --- | --- |
| `Content-Type` | `application/vnd.apache.arrow.stream` |
| `X-Jevaro-Row-Count` | Number of input states |
| `X-Accel-Buffering` | `no` |

There is no `Content-Length`. HTTP/1.1 uses chunked transfer.

After validating the whole request, Jevaro sends the schema and a zero-row
batch before starting upstream calls. Each later batch holds the next row plus
any later rows that have already finished.
Columns follow the question order in the request; rows follow state order,
even if upstream calls finish out of order. A slow earlier call delays later
rows. The [schema](arrow-schema.md) holds the shared answer metadata.

The full request is held in memory. Pending calls and results are bounded
by the [concurrency setting](../jevaro-server/README.md#limits). Closing the
HTTP connection cancels pending work.

## Errors

| When | Result |
| --- | --- |
| Invalid request | HTTP 422 with a JSON `detail` field |
| Missing key or malformed bearer header | HTTP 401 with a JSON `detail` field |
| Upstream failure after streaming starts | Aborted HTTP body; status remains 200 |

An invalid upstream API key is detected after the stream starts. It therefore
causes an aborted stream, rather than a new HTTP 401 response from Jevaro.

The server uses TypeSafe's default SDK retries, including 429 and 529 backoff,
and lowers the number of calls in flight while TypeSafe returns 429 or 529.
If retries fail, it stops the stream without null rows or JSON error records.
The successful Arrow end marker is sent only on normal completion.

Both SDKs propagate read failures and reject a completed stream with the wrong
row count. Treat the result as complete only after reading to the end without
an error. A partial file may contain valid rows from before a failure.

## Save with curl

With the server's API key set:

```sh
curl --fail-with-body --no-buffer http://127.0.0.1:8000/v1/systemone \
  -H 'Content-Type: application/json' \
  --data '{"states":["Please refund the shoes.","Where is my parcel?"],"questions":{"refund":{"type":"noul","instructions":"Is a refund being requested?"}}}' \
  --output http-results.arrows
```

On success, read the file with `pyarrow.ipc.open_stream`. If curl fails, the
output may be a JSON error or an incomplete stream.
