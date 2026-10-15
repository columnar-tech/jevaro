# Jevaro JavaScript SDK

Send a batch of states and one shared set of questions to [Jev](https://docs.typesafe.ai/)
through the [Jevaro batching proxy](https://github.com/columnar-tech/jevaro/blob/main/jevaro-server/README.md)
and read the answers in input order as an [Apache Arrow](https://arrow.apache.org/)
stream.

Part of [Jevaro](https://github.com/columnar-tech/jevaro), an experiment in bulk
inference. The batching proxy handles concurrent API calls, retries, and
conversion from JSON to Arrow. This SDK returns a reader with one row per
state and one column per question.

Works in modern browsers and Node.js 22+. Requires a running Jevaro server.
ESM imports, CommonJS, and TypeScript are supported; browsers can use a bundler
or an ESM CDN.
Start with the [quickstart](https://github.com/columnar-tech/jevaro/blob/main/docs/quickstart.md).

## Install

```sh
npm install jevaro
```

## Read results

```javascript
import { TypeSafeClient, noul } from "jevaro";

const client = new TypeSafeClient();
const reader = await client.systemOne({
  states: ["Please refund the shoes.", "Where is my parcel?"],
  questions: { refund: noul("Is a refund being requested?") },
});

try {
  console.log(reader.schema);
  for await (const batch of reader) {
    for (const row of batch) console.log(row.refund);
  }
} finally {
  await reader.cancel();
}
```

For CommonJS, use `const { TypeSafeClient, noul } = require("jevaro")`.

The result is an Apache Arrow `AsyncRecordBatchStreamReader`. The schema
arrives before answers. The first batch has zero rows; later batches have one
or more rows each. `await reader.readAll()` collects an array of record batches.

Full iteration releases the response. Breaking a `for await` loop cancels it.
Call `reader.cancel()` if you open a reader without iterating it. There is no
client-level `close()` method.

## Browser use

Try the [standalone HTML example](https://github.com/columnar-tech/jevaro/tree/main/jevaro-javascript/browser)
with `npx serve`: edit JSON states and questions, or choose images, then view
the streamed results in a table, with each Box drawn on its image. It loads the SDK from a CDN and includes instructions for starting
a local server with CORS.

Bundle the import above with your app, then set the server address explicitly:

```javascript
const client = new TypeSafeClient({ baseURL: window.location.origin });
```

This assumes your web server forwards `/v1/systemone` to Jevaro without
buffering. Set `TYPESAFE_API_KEY` on the Jevaro server so the key stays there.
For a server on a different origin, configure CORS for your app's origin;
Jevaro does not enable CORS by default.

The browser needs streaming `fetch` and `AbortSignal.any`. Node.js is not
required at runtime in the browser.

## Questions and arguments

`choice`, `score`, `noul`, `box`, `image`, `Image`, and `withAssets` are
re-exported from the official TypeSafe SDK. Plain question objects work too.
See the bundled
[three-type example](https://github.com/columnar-tech/jevaro/blob/main/jevaro-javascript/example.mjs).

`await client.systemOne(request, options)` accepts:

| Request field | Meaning |
| --- | --- |
| `state` | One string, object, array, or [image](#images-and-box-questions) |
| `states` | A nonempty array of states, or an [Arrow `Table`](#arrow-states) |
| `stateColumn` | With a `Table`, the column whose values are the states |
| `questions` | A nonempty map of question IDs to definitions |
| `model` | Override the client's default model |
| `rowsPerCall` | States per upstream call, 1 to 256; above 1, [packs states](#pack-states-into-fewer-calls) |
| `assets` | [Images under names you choose](#name-images-in-text), to write inside text |
| `boxFormat` | `"typesafe"` (default) or `"geoarrow"`: how [Box answers](#box-answers) store their edges |

Supply exactly one of `state` and `states`. An array in singular `state` is
one evaluation. Use `states` to evaluate its elements separately.

The optional second argument accepts `headers`, `timeout` in milliseconds,
and an `AbortSignal` as `signal`. A signal can cancel after the schema arrives.

## Arrow states

`states` also accepts an Apache Arrow `Table`. The SDK sends it to Jevaro as
an Arrow IPC stream. Each row becomes one state.

Without `stateColumn`, each state is an object of the row's columns. With
`stateColumn`, each state is that column's value, and the other columns are
not sent:

```javascript
import { tableFromArrays } from "apache-arrow";
import { TypeSafeClient, noul } from "jevaro";

const client = new TypeSafeClient();
const tickets = tableFromArrays({
  id: Int32Array.from([101, 102]),
  subject: ["Shoes", "Parcel"],
  body: ["Please refund the shoes.", "Where is my parcel?"],
});

// First state: {"id": 101, "subject": "Shoes", "body": "Please refund the shoes."}
const rows = await client.systemOne({
  states: tickets,
  questions: { refund: noul("Does the customer's `body` ask for a refund?") },
});
for await (const batch of rows) {
  for (const row of batch) console.log(row.refund);
}

// First state: "Please refund the shoes."
const bodies = await client.systemOne({
  states: tickets,
  stateColumn: "body",
  questions: { refund: noul("Is a refund being requested?") },
});
for await (const batch of bodies) {
  for (const row of batch) console.log(row.refund);
}
```

Use typed arrays such as `Int32Array` for integer columns. A plain array of
numbers becomes a float column, so `101` would be sent as `101.0`.

To send some columns but not others, put them in a struct column and name it
as the state column. Here `id` stays in the table but out of the states.
Answer rows follow input rows, so the row index joins each answer to its
ticket:

```javascript
const tickets = tableFromArrays({
  id: Int32Array.from([101, 102]),
  ticket: [
    { subject: "Shoes", body: "Please refund the shoes." },
    { subject: "Parcel", body: "Where is my parcel?" },
  ],
});

// First state: {"subject": "Shoes", "body": "Please refund the shoes."}
const reader = await client.systemOne({
  states: tickets,
  stateColumn: "ticket",
  questions: { refund: noul("Does the ticket's `body` ask for a refund?") },
});
let index = 0;
for await (const batch of reader) {
  for (const row of batch) console.log(tickets.get(index++).id, row.refund);
}
```

A state column can also hold lists, maps, or `arrow.json` values. See the
[HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md#arrow-request)
for how Arrow types become JSON.

Create the `Table` with `apache-arrow` 21.2.0, the version this SDK uses. Its
CommonJS and ES module builds both work; a `Table` from another version is
rejected with a `TypeError`.

## Images and Box questions

States and questions can include images, and a `box` question finds where
something is in one. TypeSafe calls these features
[dreamer-ready](https://docs.typesafe.ai/images): fit for prototypes, not yet
for production. Send the `TypeSafe-Danger-Zone-Images: true` header, which
Jevaro passes on to TypeSafe. `box` needs Jev 1.14, so use `jev-preview`:

```javascript
import { openAsBlob } from "node:fs";
import { TypeSafeClient, box, image, noul } from "jevaro";

const client = new TypeSafeClient({ defaultModel: "jev-preview" });
const reader = await client.systemOne({
  states: [
    { id: 1, photo: image(await openAsBlob("kitchen.jpg")) },
    { id: 2, photo: image(await openAsBlob("office.png")) },
  ],
  questions: {
    mug: box("the coffee mug in `photo`"),
    mine: noul({ question: "Is the mug in `photo` the one in `example`?", example: image(await openAsBlob("my-mug.jpg")) }),
  },
}, { headers: { "TypeSafe-Danger-Zone-Images": "true" } });

for await (const batch of reader) {
  for (const row of batch) console.log(JSON.stringify({ mug: row.mug, mine: row.mine }));
}
```

Each row has the box's edges, as fractions of the image's height and width,
and the Noul:

```json
{"mug":{"box":{"top":0.25,"bottom":0.82,"left":0.44,"right":0.84},"confidence":0.92},"mine":0.87}
{"mug":{"box":{"top":0.31,"bottom":0.64,"left":0.12,"right":0.35},"confidence":0.88},"mine":0.08}
```

The SDK writes each image as its reference, `String(image)`, such as
`<image:3fa85f64...>`, and uploads the file once, in its own part of the
request. Jevaro sends each upstream call the images that its state and the
questions refer to: here, one photo and `my-mug.jpg`. Create an image from a
`Blob`, such as a `File`, from bytes with `image()`, or from base64 text with
`Image.fromBase64()`. Images from the SDK's ES module and CommonJS builds both
work. In a browser, `image(file)` sends a file the user chose.

To send the header with every request, create the client with
`defaultHeaders: { "TypeSafe-Danger-Zone-Images": "true" }`. Jevaro passes on
every `TypeSafe-Danger-Zone-*` header, including
`TypeSafe-Danger-Zone-Long-Context`. `rowsPerCall` can't be used with images
or Box questions.

### Images in Arrow states

In an [Arrow `Table`](#arrow-states), each binary value is an image. Jevaro
writes a reference in its place and sends the image with that row's call:

```javascript
import { readFileSync } from "node:fs";
import { Binary, Int32, Table, vectorFromArray } from "apache-arrow";

const photos = new Table({
  id: vectorFromArray([1, 2], new Int32()),
  photo: vectorFromArray([readFileSync("kitchen.jpg"), readFileSync("office.png")], new Binary()),
});
// First state: {"id": 1, "photo": "<image:5d1c2a7e0b9f4c3d8e6a000000000000>"}
const boxes = await client.systemOne({
  states: photos, questions: { mug: box("the coffee mug in `photo`") },
}, { headers: { "TypeSafe-Danger-Zone-Images": "true" } });
```

With `stateColumn: "photo"`, each state is the image alone; write instructions
such as `"the coffee mug"`.

### Name images in text

To write an image inside text, use `withAssets`, or pass it in `assets` under
a name of your choice and write the name where the image belongs:

```javascript
import { withAssets } from "jevaro";

const myMug = image(await openAsBlob("my-mug.jpg"));
const kitchen = { id: 1, photo: image(await openAsBlob("kitchen.jpg")) };
const headers = { "TypeSafe-Danger-Zone-Images": "true" };

// The same question two ways:
await client.systemOne({ states: [kitchen], questions: { mine: noul(withAssets`Is the mug in \`photo\` this one: ${myMug}?`) } }, { headers });
await client.systemOne({
  states: [kitchen], questions: { mine: noul("Is the mug in `photo` this one: <my-mug>?") }, assets: { "<my-mug>": myMug },
}, { headers });
```

Use names that can't appear in your text by accident. Every name must appear
in a state, instructions, or criteria.

### Box answers

By default, a Box column keeps TypeSafe's edge names: `row.mug.box.left`.
With `boxFormat: "geoarrow"`, its `box` field is a
[GeoArrow](https://geoarrow.org/) box, `{xmin, ymin, xmax, ymax}`:

```javascript
const geo = await client.systemOne({
  states: photos, questions: { mug: box("the coffee mug in `photo`") }, boxFormat: "geoarrow",
}, { headers: { "TypeSafe-Danger-Zone-Images": "true" } });
// The first row's JSON.stringify(row.mug.box): {"xmin":0.44,"ymin":0.25,"xmax":0.84,"ymax":0.82}
```

`confidence` is `null` when TypeSafe doesn't report one. See the
[Arrow schema](https://github.com/columnar-tech/jevaro/blob/main/docs/arrow-schema.md#box)
for both layouts.

TypeSafe checks each image: PNG or JPEG, 256 to 1,568 pixels on each side, and
its [other limits](https://docs.typesafe.ai/images#limits). An image that it
rejects ends the stream at that row, so
[prepare images](https://docs.typesafe.ai/images#preparing-images) first.
See the [HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md#images-and-box-questions)
for which images each call sends and the checks Jevaro makes first.

## Pack states into fewer calls

`rowsPerCall` puts up to that many states in each upstream call:

```js
const reader = await client.systemOne({ states: messages, questions, rowsPerCall: 64 });
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

| Option | Default |
| --- | --- |
| `apiKey` | `TYPESAFE_API_KEY`; otherwise the server may supply its key |
| `baseURL` | `TYPESAFE_BASE_URL`, then `http://127.0.0.1:8000` |
| `defaultModel` | `TYPESAFE_DEFAULT_MODEL`, then `jev-latest` |
| `timeout` | 60,000 milliseconds to receive the schema |
| `defaultHeaders` | No extra headers |
| `fetch` | Global `fetch` |

Environment variable defaults apply in Node.js. In browsers, pass options
directly to the constructor.

The timeout ends when the schema arrives. Use `signal` to set a deadline for
reading the whole response.

## Metadata

The schema uses Arrow extension metadata for Choice, Noul, Score, and Box.
Capture `reader.schema` before iteration; Arrow may clear it when the reader
closes. Choice selections index the field's shared labels:

```javascript
// With the "department" Choice from example.mjs:
const field = reader.schema.fields.find(f => f.name === "department");
const { labels } = JSON.parse(field.metadata.get("ARROW:extension:metadata"));
// Resolve a row's choice with labels[row.department.choice].
```

Score legends and probability positions also come from field metadata.
Labels and legends are stored once in the schema. Noul stores the probability
of yes, and Box stores four edges and a confidence.
See the [Arrow schema](https://github.com/columnar-tech/jevaro/blob/main/docs/arrow-schema.md).

## Errors and compatibility

Invalid argument combinations reject with `TypeError`. HTTP errors before
streaming reject with `APIError`, which has `status` and `body` properties.
Transport, decoding, and row-count errors reject while reading.

This SDK implements System One evaluation with Arrow results. Model listing,
JSON answer objects, `APIPromise` helpers, and the official SDK's retry/error
API are outside its API. Per-state retries happen at the batching proxy;
this SDK does not retry an entire batch.

See [Contributing](https://github.com/columnar-tech/jevaro/blob/main/CONTRIBUTING.md)
for tests.

[Apache-2.0](https://github.com/columnar-tech/jevaro/blob/main/LICENSE).
Copyright 2026 Columnar Technologies Inc.
