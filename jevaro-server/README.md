# Jevaro server

A Python proxy for sending batches of states to [Jev](https://docs.typesafe.ai/)
in one request and streaming the answers as Arrow.

Send one set of questions for the whole batch. Jevaro makes parallel calls
to Jev, one per state, and streams the answers in input order. See the
[quickstart](https://github.com/columnar-tech/jevaro/blob/main/docs/quickstart.md)
for an example using both SDKs.

## Run

Requires Python 3.11+.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install jevaro-server
export TYPESAFE_API_KEY="your-api-key"
.venv/bin/jevaro-server
```

The default address is `http://127.0.0.1:8000`. Change it with `--host` and
`--port`. `python -m jevaro_server` runs the same command in an activated
environment.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `8000` | Listen port |
| `TYPESAFE_API_KEY` | Unset | Upstream key when a request has no bearer token |
| `TYPESAFE_UPSTREAM_URL` | `https://api.typesafe.ai` | Upstream API base URL |
| `JEVARO_CONCURRENCY` | `8` | Maximum pending calls/results per incoming request; positive integer |

An incoming `Authorization: Bearer <key>` overrides the server's key and is
forwarded to the TypeSafe API. A request without that header uses the server's key.
Jevaro has no separate client access control: keep it local, or put
authentication in front of a shared server.

`TYPESAFE_BASE_URL` is an SDK setting for reaching Jevaro. The server uses
`TYPESAFE_UPSTREAM_URL` to reach the TypeSafe API.

## Streaming

`POST /v1/systemone` accepts a nonempty `states` array and one `questions` map
for the whole batch. Use `state` for a single evaluation.

After validating the request, the server sends the schema and an empty batch
before starting upstream calls. Each later batch contains one answer row,
in input order.

The response uses `application/vnd.apache.arrow.stream` and has no
`Content-Length`. It sets `X-Accel-Buffering: no`; configure any reverse proxy
to pass the stream without buffering.

See the [HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md)
and [Arrow schema](https://github.com/columnar-tech/jevaro/blob/main/docs/arrow-schema.md)
for the request and result formats.

## Limits

The full JSON request is held in memory. Pending calls and completed answers
waiting for their turn are bounded by `JEVARO_CONCURRENCY`. A slow earlier
call delays later rows. Closing the stream cancels pending work; calls already
sent upstream may still finish and count as API use.

All incoming requests share one connection pool to the TypeSafe API. It uses
HTTP/2 when available, so parallel calls share a warm connection.

Retries use the official TypeSafe SDK's default policy, including backoff for
429 and 529 responses. Concurrency is per incoming request. This version has
no shared account rate limiter.

If an upstream call fails after retries, the HTTP stream aborts. Its status
is already 200 at that point. Readers must consume the stream successfully
before treating the result as complete.

## Development

See [Contributing](https://github.com/columnar-tech/jevaro/blob/main/CONTRIBUTING.md)
for setup and tests. The server is independent of the earlier demo scripts.

[Apache-2.0](https://github.com/columnar-tech/jevaro/blob/main/LICENSE).
Copyright 2026 Columnar Technologies Inc.
