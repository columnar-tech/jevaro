# HTTP API

Jevaro accepts TypeSafe System One questions and returns an Arrow IPC stream.
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
batch before starting upstream calls. Each later batch contains one row.
Columns follow the question order in the request; rows follow state order,
even if upstream calls finish out of order. A slow earlier call delays later
rows. The [schema](arrow-schema.md) holds the shared answer metadata.

The full JSON request is held in memory. Pending calls and results are bounded
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

The server uses TypeSafe's default SDK retries, including 429 and 529 backoff.
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
