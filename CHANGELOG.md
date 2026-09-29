# Changelog

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
