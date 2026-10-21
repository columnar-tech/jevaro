# Changelog

## Unreleased

- The server no longer ends a stream because one upstream call got more 429
  or 529 responses than `JEVARO_MAX_RETRIES`. It sends such a call again after
  TypeSafe's `retry-after-ms` for as long as other calls with the same API key
  succeed. Only after 30 seconds without a success does the response go back
  to the SDK.
  - In live runs of 100,000 states with `rows_per_call: 64`, TypeSafe rejected
    33–40% of attempts, and 2 of 7 runs aborted when one call was rejected 6
    times in a row. With the fix, runs completed at 2,258 and 2,378 states per
    second, even with `JEVARO_MAX_RETRIES=0`.

## 0.5.0 — 2026-10-16

All three packages are released at `0.5.0`. They support the
[dreamer-ready](https://docs.typesafe.ai/images) features of Jev 1.14: images
and Box questions.

- The server accepts Box questions, which find where something is in an
  image. Each answer is a `jevaro.box` column: the box's `top`, `bottom`,
  `left`, and `right` edges, as fractions of the image's height and width,
  and a confidence that may be null. With `"box_format": "geoarrow"`, the
  edges are a `geoarrow.box`. Box needs `jev-preview` or `jev-1.14.0`.
- States, instructions, and criteria can include images: base64 in `assets`,
  file parts of a multipart request, or binary values in Arrow states. Each
  upstream call carries only the images that its state and questions use.
  - In a live test, 2,000 small generated PNGs, each with a Box and a Noul
    question, streamed at 1,130 images per second. Box edges were within 0.01
    of the drawn rectangles at the median.
- The server forwards every `TypeSafe-Danger-Zone-*` request header to
  TypeSafe, including `TypeSafe-Danger-Zone-Long-Context`. Requests with
  images or Box questions need `TypeSafe-Danger-Zone-Images: true` and can't
  use `rows_per_call` above 1; the server rejects them with 422 before any
  upstream call, as it does unreferenced or malformed assets.
- The Python SDK accepts `Image` objects in states and questions, plus
  `assets` and `box_format`, and re-exports `Box` and `Image`.
- The JavaScript SDK accepts `image()` objects and `withAssets` text, plus
  `assets` and `boxFormat`, and re-exports `box`, `image`, `Image`, and
  `withAssets`.
- Breaking: the JavaScript SDK requires Node.js 22 or newer, as
  `@typesafe-ai/sdk` 0.7.0 does.
- The Python packages use `typesafe-sdk` 0.7.5, which compresses upstream
  request bodies of 1 KiB or more with zstd. This shrinks packed calls, whose
  states repeat in every question. In back-to-back live tests of a
  10,000-state job with `rows_per_call: 64`, it ran 1.7 times as fast as 0.4.0
  over a slower connection, and about the same on 1 Gb/s fiber. Throughput
  with one state per call was unchanged.
- The browser example can send images, and draws each Box on its image.
- `read_results.py` decodes Box answers, and the live smoke test asks a Box
  question about a generated image through each SDK.

## 0.4.0 — 2026-10-05

All three packages are released at `0.4.0`.

- The server accepts `rows_per_call`, from 1 to 256, in JSON requests and in
  the Arrow form's `request` part. Above 1, it packs up to that many states
  into each upstream call, putting each state into its own copy of every
  question's instructions.
  - In a live test of short messages with three questions, throughput rose
    from 1,040 to 2,000 states per second, using 28% fewer input tokens.
  - Answers shift slightly from one state per call, and long states with many
    questions use more tokens.
  - Instructions that conflict with packing are rejected with 422. See the
    [HTTP API](docs/http-api.md#pack-states-into-fewer-calls).
- The Python SDK accepts `rows_per_call`, and the JavaScript SDK accepts
  `rowsPerCall`.
- The server spreads upstream calls over `JEVARO_UPSTREAM_CONNECTIONS` HTTP/2
  connections. The default is 4; before, it used one. The default
  `JEVARO_CONCURRENCY` is 512, up from 256. Together, these raised
  one-state-per-call throughput in a live test from 500 to 1,040 states per
  second.
- The server lowers the number of upstream calls in flight for an API key
  while TypeSafe returns 429 or 529, and raises it again as calls succeed.
- The live smoke test also packs three states into one call.

## 0.3.1 — 2026-10-05

All three packages are released at `0.3.1`.

- Fix: the JavaScript SDK writes an Arrow `Table` with the `apache-arrow` build
  that created it. In 0.3.0, a `Table` from the ES module build, as in
  `import { tableFromArrays } from "apache-arrow"` in Node.js, lost its
  dictionary index types, so the server rejected its string columns.
- Fix: the server responds with 422, not 500, to Arrow IPC data with
  malformed metadata.
- The browser example can send states as Arrow IPC data, with an optional
  state column. It loads `jevaro` and `apache-arrow` from one esm.sh module graph.
- The live smoke test also sends Arrow IPC through each SDK.
- The HTTP API doc and READMEs show what each Arrow row becomes, with and
  without `state_column`, including struct columns, joining answers to the
  input, and Polars.

## 0.3.0 — 2026-10-05

All three packages are released at `0.3.0`.

- The server accepts states as Arrow: a `multipart/form-data` request with a
  JSON `request` part and an Arrow IPC `states` part. Each row becomes an
  object of its columns, or the value in `state_column`. Every row is checked
  before streaming starts. The server now depends on `python-multipart`.
- The Python SDK sends tabular Arrow data passed as `states`, including any
  object with `__arrow_c_stream__`, and accepts `state_column`.
- The JavaScript SDK sends an Arrow `Table` passed as `states`, and accepts
  `stateColumn`.

## 0.2.0 — 2026-09-29

All three packages are released at `0.2.0`.

- The server shares one HTTP/2 connection pool to the TypeSafe API across
  requests, instead of opening new connections for each request.
- The default `JEVARO_CONCURRENCY` is 256, up from 8. In a live test of
  10,000 states it raised throughput from 38 to 464 states per second.
- Each Arrow record batch holds every answer row that is ready in order, not
  one row per batch.
- `JEVARO_MAX_RETRIES` sets retries per upstream call. The default is 5; it was
  the SDK's default of 2.
- A standalone browser example submits batches and displays streaming Arrow
  results, including Choice labels and Score legends from the schema.
- READMEs explain Jevaro as an experimental batching proxy. Python package
  examples use `uv` and `uvx`.

## 0.1.1 — 2026-09-29

Python packages only: update the PyPI READMEs and package descriptions to use
Jev, lead with batching, and link to the server README. Runtime code is unchanged.

## 0.1.0 — 2026-09-29

The server and Python SDK are available on PyPI. The JavaScript SDK is
available on npm.

- Python proxy accepts one state or many and streams Arrow results in input order.
- Choice labels and Score legends are shared through Arrow schema metadata.
- Python SDK provides sync and async readers.
- JavaScript SDK supports browsers and Node.js, with async readers and TypeScript declarations.
- Streams support cancellation and report incomplete results.
- TypeSafe's SDK handles upstream retries, including 429 and 529 responses.
- Examples, metadata-only file reader, package checks, and CI are included.
