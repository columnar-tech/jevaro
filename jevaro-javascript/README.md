# Jevaro JavaScript SDK

Send a batch of states and one set of questions to [Jev](https://docs.typesafe.ai/)
through a [Jevaro server](https://github.com/columnar-tech/jevaro/blob/main/jevaro-server/README.md).
Read the answers as an Arrow stream, in input order.

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
| `states` | A nonempty array of states |
| `questions` | A nonempty map of question IDs to definitions |
| `model` | Override the client's default model |

Supply exactly one of `state` and `states`. An array in singular `state` is
one evaluation. Use `states` to evaluate its elements separately.

The optional second argument accepts `headers`, `timeout` in milliseconds,
and an `AbortSignal` as `signal`. A signal can cancel after the schema arrives.

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

Capture `reader.schema` before iteration; Arrow may clear it when the reader
closes. Choice selections index the field's shared labels:

```javascript
// With the "department" Choice from example.mjs:
const field = reader.schema.fields.find(f => f.name === "department");
const { labels } = JSON.parse(field.metadata.get("ARROW:extension:metadata"));
// Resolve a row's choice with labels[row.department.choice].
```

Score legends and probability positions also come from field metadata.
See the [Arrow schema](https://github.com/columnar-tech/jevaro/blob/main/docs/arrow-schema.md).

## Errors and compatibility

Invalid argument combinations reject with `TypeError`. HTTP errors before
streaming reject with `APIError`, which has `status` and `body` properties.
Transport, decoding, and row-count errors reject while reading.

This SDK implements System One evaluation with Arrow results. Model listing,
JSON answer objects, `APIPromise` helpers, and the official SDK's retry/error
API are outside its API. Per-state retries happen at the proxy; this SDK does
not retry an entire batch.

See [Contributing](https://github.com/columnar-tech/jevaro/blob/main/CONTRIBUTING.md)
for tests.

[Apache-2.0](https://github.com/columnar-tech/jevaro/blob/main/LICENSE).
Copyright 2026 Columnar Technologies Inc.
