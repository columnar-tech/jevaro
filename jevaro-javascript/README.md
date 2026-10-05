# Jevaro JavaScript SDK

Send a batch of states and one shared set of questions to [Jev](https://docs.typesafe.ai/)
through the [Jevaro batching proxy](https://github.com/columnar-tech/jevaro/blob/main/jevaro-server/README.md)
and read the answers in input order as an [Apache Arrow](https://arrow.apache.org/)
stream.

Part of [Jevaro](https://github.com/columnar-tech/jevaro), an experiment in bulk
inference. The batching proxy handles concurrent API calls, retries, and
conversion from JSON to Arrow. This SDK returns a reader with one row per
state and one column per question.

Works in modern browsers and Node.js 20.3+. Requires a running Jevaro server.
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
with `npx serve`: edit JSON states and questions, then view the streamed results
in a table. It loads the SDK from a CDN and includes instructions for starting
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

`choice`, `score`, and `noul` are re-exported from the official TypeSafe SDK.
Plain question objects work too. See the bundled
[three-type example](https://github.com/columnar-tech/jevaro/blob/main/jevaro-javascript/example.mjs).

`await client.systemOne(request, options)` accepts:

| Request field | Meaning |
| --- | --- |
| `state` | One string, object, or array |
| `states` | A nonempty array of states, or an [Arrow `Table`](#arrow-states) |
| `stateColumn` | With a `Table`, the column whose values are the states |
| `questions` | A nonempty map of question IDs to definitions |
| `model` | Override the client's default model |

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

The schema uses Arrow extension metadata for Choice, Noul, and Score.
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
of yes.
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
