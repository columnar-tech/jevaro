# Jevaro server

A Python batching proxy for [Jev](https://docs.typesafe.ai/) that accepts many
states in one request and streams the answers as [Apache Arrow](https://arrow.apache.org/).

Jevaro is an experiment in bulk inference. Send a `states` array and one
shared `questions` map. The batching proxy calls the
[TypeSafe API](https://docs.typesafe.ai/api) concurrently, once per state or,
with [`rows_per_call`](#pack-states-into-fewer-calls), for many states at once.
It converts the JSON answers into Arrow IPC. The schema arrives first,
followed by answers in input order: one row per state and one column per
question.

Arrow extension types preserve Choice, Noul, and Score answers, with shared
labels and legends stored once in the schema. See the
[quickstart](https://github.com/columnar-tech/jevaro/blob/main/docs/quickstart.md)
for an example using both SDKs.

## Run

Requires Python 3.11+. Start the server with [uvx](https://docs.astral.sh/uv/guides/tools/):

```sh
export TYPESAFE_API_KEY="your-api-key"
uvx jevaro-server
```

The default address is `http://127.0.0.1:8000`. Change it with `--host` and
`--port`, for example `uvx jevaro-server --port 8001`.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `8000` | Listen port |
| `TYPESAFE_API_KEY` | Unset | Upstream key when a request has no bearer token |
| `TYPESAFE_UPSTREAM_URL` | `https://api.typesafe.ai` | Upstream API base URL |
| `JEVARO_CONCURRENCY` | `512` | Maximum pending calls/results per incoming request; positive integer |
| `JEVARO_MAX_RETRIES` | `5` | Retries per upstream call after its first attempt; nonnegative integer |
| `JEVARO_UPSTREAM_CONNECTIONS` | `4` | HTTP/2 connections to the TypeSafe API; positive integer |

An incoming `Authorization: Bearer <key>` overrides the server's key and is
forwarded to the TypeSafe API. A request without that header uses the server's key.
Jevaro has no separate client access control: keep it local, or put
authentication in front of a shared server.

`TYPESAFE_BASE_URL` is an SDK setting for reaching Jevaro. The server uses
`TYPESAFE_UPSTREAM_URL` to reach the TypeSafe API.

## Streaming

`POST /v1/systemone` accepts a nonempty `states` array and one `questions` map
for the whole batch. Use `state` for a single evaluation.

To send the states as Arrow instead, POST `multipart/form-data` with a JSON
`request` part and an Arrow IPC `states` part. Each row becomes one state:
an object of the row's columns, or the value in `state_column`. For a table
with `id` and `body` columns:

| `request` part | First state |
| --- | --- |
| `{"questions": {...}}` | `{"id": 101, "body": "Please refund the shoes."}` |
| `{"questions": {...}, "state_column": "body"}` | `"Please refund the shoes."` |

The [HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md#arrow-request)
has a curl example and shows how Arrow types become JSON.

After validating the request, the server sends the schema and an empty batch
before starting upstream calls. Each later batch holds the next answer row
plus any later rows that have already finished, in input order.

The response uses `application/vnd.apache.arrow.stream` and has no
`Content-Length`. It sets `X-Accel-Buffering: no`; configure any reverse proxy
to pass the stream without buffering.

See the [HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md)
and [Arrow schema](https://github.com/columnar-tech/jevaro/blob/main/docs/arrow-schema.md)
for the request and result formats.

## Pack states into fewer calls

Add `"rows_per_call": 64` to a request to evaluate up to 64 states in each
upstream call. Each state goes into its own copy of every question's
instructions, so states can't affect each other's answers.

In a live test of short support messages with three questions, throughput
went from 1,040 to 2,000 states per second. TypeSafe's token rate limit set
that ceiling; a 10,000-state job ran at 9,600 per second. Short states with
few questions also use 28% fewer input tokens.

There are two tradeoffs:

- **Answers shift slightly, and consistently.** About 1.5% of Choice answers
  differed from one state per call.
- **Long states with many questions cost more.** Each question carries its
  own copy of the state, so they use more input tokens and can be slower.

The [HTTP API](https://github.com/columnar-tech/jevaro/blob/main/docs/http-api.md#pack-states-into-fewer-calls)
shows what Jevaro sends upstream, the measurements, and the requests it rejects.

## Limits

The full request is held in memory. An Arrow upload stays as Arrow data, and
its rows are converted to JSON as upstream calls start. Pending calls and completed answers
waiting for their turn are bounded by `JEVARO_CONCURRENCY`. A slow earlier
call delays later rows. Closing the stream cancels pending work; calls already
sent upstream may still finish and count as API use.

All incoming requests share `JEVARO_UPSTREAM_CONNECTIONS` HTTP/2 connections
to the TypeSafe API, and each call goes on the least busy one. TypeSafe allows
100 calls at a time per connection, so the default 4 connections carry up to
400 calls at once across all requests; the rest wait in Jevaro. In a live
test of one state per call, the defaults of 4 connections and concurrency 512
ran at 1,040 states per second, against 500 with one connection and
concurrency 256.

The default concurrency is higher than the number of connection slots, so
waiting calls can fill slots as soon as they free up while finished rows wait
for earlier ones. With `rows_per_call`, concurrency counts upstream calls
rather than states.

Retries use the official TypeSafe SDK's retry policy with `JEVARO_MAX_RETRIES`
retries, including backoff for 429 and 529 responses and dropped connections.
The SDK stops retrying a call after 30 seconds. Concurrency is per incoming
request.

Jevaro also adapts to TypeSafe's rate limits for each API key, across all
requests:

- **On 429 or 529**, it cuts the calls in flight to 70% of the current number.
  It does this at most twice a second.
- **As calls succeed**, it raises the number again by about one call per
  round trip.

If an upstream call fails after retries, the HTTP stream aborts. Its status
is already 200 at that point. Readers must consume the stream successfully
before treating the result as complete.

## Development

See [Contributing](https://github.com/columnar-tech/jevaro/blob/main/CONTRIBUTING.md)
for setup and tests. The server is independent of the earlier demo scripts.

[Apache-2.0](https://github.com/columnar-tech/jevaro/blob/main/LICENSE).
Copyright 2026 Columnar Technologies Inc.
