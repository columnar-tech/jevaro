# HTTP API

Jevaro accepts TypeSafe System One questions and returns an Arrow IPC stream.
Send the states as JSON, or as an [Arrow IPC stream](#arrow-request). States
and questions can include [images](#images-and-box-questions).
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
| `assets` | Map of references to [images](#send-images-as-json), as TypeSafe accepts them |
| `box_format` | `typesafe` (default) or `geoarrow`: how [Box answers](arrow-schema.md#box) store their edges |

Supply exactly one of `state` and `states`. `state: ["a", "b"]` is one
evaluation; `states: ["a", "b"]` is two. An individual state cannot be a
number, boolean, or null. Unknown top-level fields are rejected.

Questions use the [TypeSafe definitions](https://docs.typesafe.ai/api):

| `type` | `criteria` |
| --- | --- |
| `choice` | Map of 1–255 labels to descriptions; a description may be null |
| `score` | Ordered array of 2–10 level descriptions |
| `noul` | Optional object describing `true` and `false` outcomes |
| `box` | None; a [Box question](#box-questions) has only `instructions` |

Instructions and descriptions can use strings, objects, or arrays. The proxy
accepts omitted or null instructions, as the SDK question helpers do.
Every state uses the same questions and model.

## Arrow request

To send states as Arrow, POST `multipart/form-data` with two parts:

| Part | Content-Type | Contents |
| --- | --- | --- |
| `request` | `application/json` | `questions`, `model`, and optional `state_column`, `rows_per_call`, `assets`, and `box_format` |
| `states` | `application/vnd.apache.arrow.stream` | An Arrow IPC stream with one row per state |

The `request` part takes the same fields as a JSON request, plus
`state_column`. With a `states` part, it rejects `state` and `states`. A part
may omit its Content-Type; `states` may also use `application/octet-stream`.
Any other part must be an [image](#send-images-as-file-parts) that `assets`
names. The response is the same Arrow stream as for a JSON request.

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

A state column can hold strings, images, structs, lists, maps, or
`arrow.json` values. Each `arrow.json` value is parsed, so states can differ
in shape from row to row. A state column cannot contain nulls. A binary
value is an [image](#send-images-as-arrow-binary-values). Answer rows follow input
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
| Binary, large binary, binary view | An [image](#send-images-as-arrow-binary-values), by its reference |
| Null | `null` |

A null inside a row becomes `null`. Other types, including fixed-size binary,
list views, durations, and unions, are rejected. Send the IPC stream format; the IPC file
format is rejected.

### Validation and memory

The server checks the API key before reading the body. It then reads the whole
upload and converts every row once, so a bad row causes a 422 before streaming
starts. The upload stays in memory as Arrow data. Rows are converted again,
1,024 at a time, or 32 at a time for a table with binary values, as upstream
calls start.

## Images and Box questions

TypeSafe calls images and Box questions
[dreamer-ready](https://docs.typesafe.ai/images): fit for prototypes, not yet
for production. To use them, send the `TypeSafe-Danger-Zone-Images: true`
header. Jevaro sends it to TypeSafe with each upstream call, and rejects a
request with images or Box questions that lacks it.

### Box questions

A Box question finds one thing in an image. It has only `instructions`:

```json
{"mug": {"type": "box", "instructions": "the coffee mug in `photo`"}}
```

Box needs Jev 1.14: send `"model": "jev-preview"` or `"jev-1.14.0"`.
`jev-latest` still names Jev 1.13, and TypeSafe rejects a Box question for it.

Each answer row holds the box's four edges, as fractions of the image's height
(`top`, `bottom`) and width (`left`, `right`), and a confidence that may be
null:

```json
{"box": {"top": 0.25, "bottom": 0.82, "left": 0.44, "right": 0.84}, "confidence": 0.92}
```

With `"box_format": "geoarrow"`, the edges are a `geoarrow.box` instead:

```json
{"box": {"xmin": 0.44, "ymin": 0.25, "xmax": 0.84, "ymax": 0.82}, "confidence": 0.92}
```

See the [Arrow schema](arrow-schema.md#box) for both layouts.

### Send images as JSON

`assets` maps each image's reference to the image, as in
[TypeSafe's API](https://docs.typesafe.ai/images#sending-images-in-a-request).
Write a reference wherever its image belongs: in a state, in instructions,
or in criteria. This request finds the mug in two photos and compares each
with a picture of a particular mug:

```json
{
  "model": "jev-preview",
  "states": [{"id": 1, "photo": "<kitchen>"}, {"id": 2, "photo": "<office>"}],
  "assets": {
    "<kitchen>": {"type": "image", "format": "jpg", "data_base64": "/9j/4AAQSkZJRg..."},
    "<office>": {"type": "image", "format": "png", "data_base64": "iVBORw0KGgo..."},
    "<my-mug>": {"type": "image", "format": "jpg", "data_base64": "/9j/4AAQSkZJRg..."}
  },
  "questions": {
    "mug": {"type": "box", "instructions": "the coffee mug in `photo`"},
    "mine": {"type": "noul", "instructions": "Is the mug in `photo` this one: <my-mug>?"}
  }
}
```

Each upstream call sends the images that its state and the questions refer to:

| Row | Upstream `state` | Images sent |
| --- | --- | --- |
| 0 | `{"id": 1, "photo": "<kitchen>"}` | `<kitchen>`, `<my-mug>` |
| 1 | `{"id": 2, "photo": "<office>"}` | `<office>`, `<my-mug>` |

Save the request as `request.json` and send it:

```sh
curl --fail-with-body --no-buffer http://127.0.0.1:8000/v1/systemone \
  -H 'Content-Type: application/json' \
  -H 'TypeSafe-Danger-Zone-Images: true' \
  --data @request.json --output boxes.arrows
```

An asset has `"type": "image"` or no `type`, and either:

- `data_base64`: the file, base64-encoded with the standard alphabet and `=`
  padding, without whitespace or a `data:` prefix, and `format`: `png`, `jpg`,
  or `jpeg`; or
- `part`: in a multipart request, the name of the
  [file part](#send-images-as-file-parts) that holds the image.

### Send images as file parts

Base64 makes each image about a third larger. To send image files as they are,
POST `multipart/form-data`: the JSON request in a `request` part, and each
image in its own part, named by its asset's `part`. These are the same
requests as above:

```sh
curl --fail-with-body --no-buffer http://127.0.0.1:8000/v1/systemone \
  -H 'TypeSafe-Danger-Zone-Images: true' \
  -F 'request={
    "model": "jev-preview",
    "states": [{"id": 1, "photo": "<kitchen>"}, {"id": 2, "photo": "<office>"}],
    "assets": {"<kitchen>": {"part": "kitchen"}, "<office>": {"part": "office"}, "<my-mug>": {"part": "my-mug"}},
    "questions": {
      "mug": {"type": "box", "instructions": "the coffee mug in `photo`"},
      "mine": {"type": "noul", "instructions": "Is the mug in `photo` this one: <my-mug>?"}
    }
  };type=application/json' \
  -F 'kitchen=@kitchen.jpg;type=image/jpeg' \
  -F 'office=@office.png;type=image/png' \
  -F 'my-mug=@my-mug.jpg;type=image/jpeg' \
  --output boxes.arrows
```

An image part has Content-Type `image/png`, `image/jpeg`, `image/jpg`, or
`application/octet-stream`, or none. Each part belongs to exactly one asset.
The `request` part holds `state` or `states`, or the request can send
[Arrow states](#arrow-request) in a `states` part.

### Send images as Arrow binary values

In [Arrow states](#arrow-request), each binary value is an image file. Jevaro
writes a new reference in its place and sends the image with that row's
upstream call. Make a table of two photos:

```python
from pathlib import Path
import pyarrow as pa

photos = pa.table({
    "id": [1, 2],
    "photo": [Path("kitchen.jpg").read_bytes(), Path("office.png").read_bytes()],
})
with pa.ipc.new_stream("photos.arrows", photos.schema) as writer:
    writer.write_table(photos)
```

Save the request fields as `request.json`:

```json
{
  "model": "jev-preview",
  "questions": {"mug": {"type": "box", "instructions": "the coffee mug in `photo`"}}
}
```

Send both parts:

```sh
curl --fail-with-body --no-buffer http://127.0.0.1:8000/v1/systemone \
  -H 'TypeSafe-Danger-Zone-Images: true' \
  -F 'request=<request.json;type=application/json' \
  -F 'states=@photos.arrows;type=application/vnd.apache.arrow.stream' \
  --output boxes.arrows
```

The two states are:

```json
{"id": 1, "photo": "<image:5d1c2a7e0b9f4c3d8e6a000000000000>"}
{"id": 2, "photo": "<image:5d1c2a7e0b9f4c3d8e6a000000000001>"}
```

Each upstream call sends one image, under the reference in its state. Every
request uses a new random prefix. With `"state_column": "photo"`, the state is
the image alone, so write instructions such as `"the coffee mug"`.

Binary values work anywhere in a row. A list of binary values becomes an array
of references, and a binary field of a struct or a binary map value becomes a
reference in its object. A null stays `null`. Map keys can't be binary, since
JSON object keys are strings. A binary column with an Arrow extension type,
such as `geoarrow.wkb`, is rejected rather than read as images. A request can
also name images for its questions in `assets` and send them as file parts
beside the `states` part.

### Which images each call sends

Images that the questions refer to go with every upstream call. Images that a
state refers to go only with that state's call. Jevaro finds references the
way TypeSafe does: a string that is exactly a reference, then references
inside longer strings, from left to right. Where several start at the same
place, such as `img` and `img2`, Jevaro matches the longest. TypeSafe matches
the one that `assets` lists first, so Jevaro lists each call's assets longest
first. Object keys, such as Choice options, are never references. Use
references that can't appear in your text by accident, such as `<image:...>`
or a UUID.

Every asset must be referenced somewhere, as TypeSafe requires. Jevaro doesn't
compress the bodies of upstream calls with images, since image files are
already compressed.

### Checks before streaming

Jevaro returns 422 before any upstream call for:

- Images or Box questions without the `TypeSafe-Danger-Zone-Images: true` header
- Images or Box questions with `rows_per_call` above 1; packing would send
  each row's images again with every question
- An asset that no state, instructions, or criteria refer to
- An asset without `part` or `data_base64`, `data_base64` without `format`,
  invalid base64, or an empty image
- A part that an asset names but the request lacks, a part that two assets
  name, a part that no asset names, or an image part with another Content-Type
- A reference that is empty, only whitespace, or over 1,024 bytes, or the
  references of one call totaling over 65,536 bytes
- An empty binary value, or binary values under an Arrow extension type

Jevaro passes image files to TypeSafe unchanged. TypeSafe checks their
contents: PNG or JPEG, at least 256 and at most 1,568 pixels on each side,
and its [other limits](https://docs.typesafe.ai/images#limits), which may
change. An image that TypeSafe rejects [ends the stream](#errors) at its row,
so [prepare images](https://docs.typesafe.ai/images#preparing-images) before
sending them.

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

**Conflicts.** Before any upstream call, Jevaro rejects three kinds of request
with 422:

- **Images or Box questions.** Packing would send each row's images again
  with every question, so send them one state per call.

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

## Danger-zone headers

TypeSafe turns on experimental features with `TypeSafe-Danger-Zone-*`
request headers. Jevaro sends every such header that it receives, unchanged,
with each upstream call for that request. Apart from the
[API key](#credentials), it sends no other request headers upstream.

| Header | At TypeSafe |
| --- | --- |
| `TypeSafe-Danger-Zone-Images: true` | Turns on [images and Box questions](#images-and-box-questions) |
| `TypeSafe-Danger-Zone-Long-Context: true` | Raises the [context length](https://docs.typesafe.ai/models#long-context) to 262k tokens per request and per question |

This request evaluates two long contracts with long context on:

```sh
curl --fail-with-body --no-buffer http://127.0.0.1:8000/v1/systemone \
  -H 'Content-Type: application/json' \
  -H 'TypeSafe-Danger-Zone-Long-Context: true' \
  --data '{"states":["<first contract>","<second contract>"],"questions":{"termination":{"type":"noul","instructions":"Can either party end the agreement early?"}}}' \
  --output contracts.arrows
```

TypeSafe says long context is slower and less accurate, so send the header
only with requests that need it. With `rows_per_call`, Jevaro still keeps each
packed call to about 48,000 estimated tokens.

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
