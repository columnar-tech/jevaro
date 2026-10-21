# Throughput with rows_per_call

How many states per second can Jevaro stream when it packs many states into
each TypeSafe call? `bench.py` starts a Jevaro server in its own process and
streams states through it with the Python SDK. The server counts every
upstream attempt, its status, its input tokens, and the calls in flight.

```sh
# Free: a local fake whose latency grows with the questions in a call
.venv/bin/python benchmarks/rows-per-call/bench.py --fake --rows 100000 --rows-per-call 64 --concurrency 512 --connections 4
# Paid: the TypeSafe API
TYPESAFE_API_KEY=... .venv/bin/python benchmarks/rows-per-call/bench.py --rows 100000 --rows-per-call 64
.venv/bin/python benchmarks/rows-per-call/summarize.py   # Table of results/*.json
```

The `messages` workload has distinct synthetic support messages of about 180
characters, with three questions: a Choice, a Score, and a Noul. The first
10,000 messages are the same as in Jevaro's earlier live benchmarks. The
`tickets` workload has structured tickets with six questions that refer to
the state by path; it comes from
[`experiments/state-packing`](../../experiments/state-packing/README.md).

States go to Jevaro as a JSON list unless you pass `--input arrow`. Then they
go as an Arrow IPC stream, as messages in one string column named by
`state_column`, or as tickets with one column per key. Only Arrow input passes
through the server's `read_states`, and the report gives its time in
`read_states_ms`:

```sh
.venv/bin/python benchmarks/rows-per-call/bench.py --fake --rows 100000 --rows-per-call 64 --concurrency 512 --connections 4 --input arrow
# "read_states_ms": [77.6]: reading, validating, and checking all 100,000 states before streaming
```

## Results against the TypeSafe API

All runs used `jev-1.13.0` on 2026-10-05, on one account.

| Run | States | Rows per call | Concurrency | Connections | States/s | Input tokens per state | Tokens/s | 429s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| One state per call, the 0.3.1 defaults | 20,000 | 1 | 256 | 1 | 499 | 475 | 237k | 0 |
| One state per call | 20,000 | 1 | 256 | 4 | 671 | 475 | 319k | 0 |
| One state per call, the new defaults | 20,000 | 1 | 512 | 4 | 1,039 | 475 | 494k | 0 |
| Packed | 100,000 | 32 | 256 | 1 | 2,022 | 345 | 697k | 412 |
| Packed | 100,000 | 64 | 256 | 1 | 1,997 | 341 | 680k | 391 |
| Packed | 100,000 | 64 | 256 | 4 | 1,998 | 341 | 681k | 210 |
| Packed, a short job | 10,000 | 64 | 512 | 4 | 9,633 | 340 | 3.27M | 0 |
| Tickets, one state per call | 10,000 | 1 | 256 | 1 | 460 | 1,053 | 484k | 0 |
| Tickets, packed | 10,000 | 32 (16 fit) | 256 | 1 | 586 | 1,885 | 1.10M | 264 |

**TypeSafe's token rate limit sets the ceiling for packed calls.** A probe of
200 simultaneous packed calls found that TypeSafe accepted about 4 million
input tokens at once. It rejected the rest with `retry-after-ms`, which looks
like a token bucket. In the 100,000-state runs, it accepted about 600–700
thousand tokens per second once that burst was spent.

So sustained throughput is about the account's token rate divided by tokens
per state. Packing helps in two ways:

- **It uses fewer tokens per state** for short states: each call's fixed
  overhead of about 260 tokens is shared.
- **It needs far fewer calls,** so the 100-call limit per HTTP/2 connection
  and per-call latency stop mattering.

A short job runs much faster while the burst lasts.

**Adapting to 429s is required.** Before the server adapted to 429s, a packed
run at concurrency 256 got 822 rejections out of 1,204 calls. Its retries ran
out, and the stream failed after 11,968 states. The server now cuts the calls
in flight when TypeSafe returns 429 or 529. The same run then completed, at
about 2,000 states per second.

**Adapting wasn't enough on its own.** On 2026-10-21, 2 of 7 runs of 100,000
states with 64 rows per call, at concurrency 256 over 4 connections, still
aborted, at 67,136 and 58,304 states. TypeSafe rejected 33–40% of all
attempts, each with a `retry-after-ms` of 800 to 1,200. About 30% of retries
were rejected again, so among the 1,563 calls of a run, one now and then drew
6 rejections in a row and used up the SDK's 5 retries. Successes never paused
for more than about a second, so TypeSafe was pacing these runs, not blocking
them.

The server now sends a call that gets 429 or 529 again itself, for as long
as other calls with the key succeed. With `JEVARO_MAX_RETRIES=0`, the old
server aborted after 4,416 states. The new one completed at 2,258 states per
second, and at 2,378 with the default of 5 retries. Up to 41% of attempts
were rejected, and some calls 5 times.

**Long states with many questions don't gain.** Each question carries its own
copy of the state, so the tickets used 1.8 times the tokens per state. The
packed run finished faster only by spending TypeSafe's burst allowance. At
the sustained token rate, it would manage about 330 states per second,
against 460 for one state per call.

## Results against the local fake

The fake allows 100 calls at a time per HTTP/2 connection, and has no rate
limit unless you pass `--fake-rate-limit BURST,TOKENS_PER_SECOND`. Each call
takes 165 ms plus 0.74 ms per question, varied by a lognormal factor with
σ = 0.3, which roughly matches TypeSafe's latency.

With a rate limit, input tokens come from a token bucket. A call that finds
too few tokens gets a 429 after 35 ms, with a `retry-after-ms` of 800 to 1,200,
as TypeSafe's do. The fake rejects fewer attempts than TypeSafe for the same
bucket. With `--fake-rate-limit 300000,700000`, it rejects about 20%, and 1
retry ends the old server's stream:

```sh
JEVARO_MAX_RETRIES=1 .venv/bin/python benchmarks/rows-per-call/bench.py --fake --fake-rate-limit 300000,700000 --rows 100000 --rows-per-call 64 --concurrency 256 --connections 4
# Before 2026-10-21: "rows": 768, "error": "RemoteProtocolError(...)"; now "rows": 100000, about 2,300 per second
```

| Run | States | Rows per call | Concurrency | Connections | States/s | Server CPU |
| --- | --- | --- | --- | --- | --- | --- |
| One state per call | 20,000 | 1 | 512 | 4 | 1,113 | 62% |
| Packed | 50,000 | 64 | 256 | 1 | 12,213 | 57% |
| Packed | 100,000 | 64 | 512 | 4 | 20,657 | 83% |

With no rate limit, one Jevaro process packs about 20,000 states per second
before its CPU is the limit. More connections or concurrency didn't help
beyond that. With one connection, the 100-call limit is the bottleneck.

## Caveats

- **One account, one day.** TypeSafe says its rate limits "are adjusting
  dynamically", so another account, or another day, may see a different
  ceiling.
- **Short runs.** Each run lasted under a minute, so the token bucket's burst
  is a large share of the shorter ones.
- **Fake latency is a model.** Packed calls carry more questions, and the
  fake's per-question latency only approximates how TypeSafe's latency grows.
