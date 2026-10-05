# Changelog

## Unreleased

- Fix: the JavaScript SDK writes an Arrow `Table` with the `apache-arrow` build
  that created it. In 0.3.0, a `Table` from the ES module build, as in
  `import { tableFromArrays } from "apache-arrow"` in Node.js, lost its
  dictionary index types, so the server rejected its string columns.
- Fix: the server responds with 422, not 500, to Arrow IPC data with
  malformed metadata.
- The browser example can send states as Arrow IPC data, with an optional
  state column. It loads `jevaro` and `apache-arrow` from one esm.sh module graph.
- The live smoke test also sends Arrow IPC through each SDK.

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
