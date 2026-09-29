# Earlier Jev examples

These scripts predate [Jevaro](../README.md). Run their commands from the
repository root. Saved data and timing logs are local artifacts.

Their Arrow files use `jev_demo.*` extensions and include the input state.
Jevaro uses `jevaro.*` extensions and stores answers only.

One Python call to Jev: give it a customer message and ask whether the customer
wants a refund. It prints the probability of yes, from 0 to 1.

Requires Python 3.10+ and a [TypeSafe API key](https://console.typesafe.ai).

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export TYPESAFE_API_KEY="your-api-key"
python example.py
```

Skip the `export` if your key is already set. Edit `state` in `example.py` to try
another message.

For all three supported answer types in one call, run:

```sh
python all_types.py
```

| Type | Example | Returned value |
| --- | --- | --- |
| Choice | Which team handles the message? | One label, plus confidence and option probabilities |
| Score | How soon does the customer want a resolution? | A weighted score from 0 to 2 for our three levels, plus confidence, level probabilities, and a legend |
| Noul | Is the customer requesting a refund? | Probability of yes, from 0 to 1 |

`all_types.py` prints each value and its supporting fields. Edit its `state` to try
another message. Scores can be fractional; Noul has no separate confidence field.

Uses the [official TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python).

The Arrow example evaluates many states against the same `QUESTIONS` exported by
`all_types.py`. It shares one synchronous client across a bounded thread pool and
returns a lazy `pyarrow.RecordBatchReader`. Each state gets a separate API request;
reading an Arrow batch evaluates up to `batch_size` states with at most 16 requests
in flight. Results stay in input order even when requests finish out of order. The
client must stay open during reading. No calls run between batches or after closing
the reader at a batch boundary.
Install the updated dependencies with `.venv/bin/python -m pip install -r requirements.txt`
or `uv pip install --python .venv/bin/python -r requirements.txt`.

Run the small demo (three states by default):

```sh
.venv/bin/python many_states.py --limit 3 --batch-size 2 --output debug_results.arrow
```

For the full corpus, with parallel requests and an Arrow IPC **stream**:

```sh
.venv/bin/python many_states.py --limit 10000 --output parallel_results.arrows --quiet
```

`--quiet` suppresses schema and per-row JSON printing. Progress and retry messages
go to stderr; the final summary reports HTTP attempts, 429/529 counts, peak requests
in flight, and input tokens reported by successful calls. An output path is opened
for writing and replaces any existing file at that path.

The [published Jev limits](https://docs.typesafe.ai/models), checked 2026-09-28, are
**1,200 requests/minute** and **250,000 tokens/second**. TypeSafe says these limits
can change dynamically. The shared controller starts 5% below them: **19 request
starts/second** and **237,500 estimated tokens/second**. Attempts are evenly spaced;
concurrency hides network latency without permitting an initial burst. Both new
requests and retries use the same budgets, maintained across Arrow batches.

`request_scheduler.py` handles [rate limits and overload](https://docs.typesafe.ai/api#handling-rate-limits):

- A **429** cuts the target request rate by 20%; a **529** cuts it by 50%. The latter
  indicates temporary server overload, not a known change in the account quota.
- All new dispatches pause for exponential backoff with jitter, or the server's
  longer `Retry-After` / `retry-after-ms` delay. HTTP dates are supported. Server
  delays are never shortened to the local backoff cap.
- A group of requests already in flight causes one rate reduction. Continued
  rejections without a successful request extend the backoff, avoiding repeated
  downward guesses while the same quota window is still exhausted.
- After a clean minute with at least ten successes, throughput increases 10%, up
  to 95% of the last rejected rate. Every five clean minutes the controller probes
  that learned ceiling upward by 5%, bounded by the configured ceiling. This lets
  it adapt when capacity recovers while usually retaining a small margin.
- Each state gets at most eight retries by default. HTTP 408, other 5xx responses,
  and connection/timeouts also retry through the controller. Other errors, such as
  401 and 422, fail immediately. SDK retries are disabled on each evaluation call,
  so they cannot bypass pacing or hide congestion feedback.

The learned rate is an empirical operating target for this workload, not an exact
measurement of the server's quota. It is retained for the current reader/run;
other processes using the account are outside this controller. The token budget
uses conservative UTF-8 payload sizes plus prompt overhead, and increases those
reservations if returned `usage.input_tokens` reveals an underestimate. It is a
rolling one-second budget, but the reservations are **not exact token counts**;
429 feedback also handles token-limit pressure. A request whose reservation is
larger than the entire configured token budget is rejected locally.

Override the ceilings for your account, or change concurrency and the retry budget:

```sh
.venv/bin/python many_states.py --limit 10000 --output parallel_results.arrows --quiet \
  --concurrency 16 --requests-per-minute 1200 --tokens-per-second 250000 --max-retries 8
```

Ctrl-C or a permanent error stops new dispatches and retries. At most `concurrency`
HTTP calls already in flight finish under the client's network timeout. Complete
Arrow batches already written remain readable; an unfinished batch is not written.

Estimate the input cost of all 10,000 states with **one** sample API request:

```sh
.venv/bin/python many_states.py --estimate-cost
```

Estimate mode reads the selected states locally, chooses the one closest to their
average character length, and sends it with the same three questions. It reads
`usage.input_tokens` and calculates:

```text
estimated input tokens = sample input tokens × number of selected states
estimated USD = estimated input tokens × USD per million input tokens / 1,000,000
```

This is an approximation: character length selects a representative message; it
does not count tokens. No official local Jev tokenizer was found in the current
SDK or docs. The measured usage includes the shared questions. SDK retries are
disabled for the probe, so estimate mode makes at most one API attempt and exits.
The JSON report shows the estimated run cost and the probe's own cost separately.

`--limit 100` estimates the first 100 states; without `--limit`, estimate mode uses
the whole file. Normal evaluation still defaults to three states. No Arrow output
is created by estimate mode, and it cannot be combined with `--output`.

The default rate is **$0.042 per million input tokens** for `jev-1.13.0`, from the
[official pricing](https://docs.typesafe.ai/models), checked 2026-09-28. Output tokens
are free for that model. The estimator checks the model resolved by the API instead
of assuming an alias keeps the same price. Override the input rate when needed:

```sh
.venv/bin/python many_states.py --estimate-cost --input-price-per-million 0.042
```

Use the reader directly:

```python
from itertools import islice
from typesafe_sdk import TypeSafeClient
from arrow_results import evaluate_states
from request_scheduler import AdaptiveRateLimiter

limiter = AdaptiveRateLimiter()  # Optional: customize ceilings and inspect statistics.
with open("states.txt", encoding="utf-8") as source, TypeSafeClient() as client:
    states = (line.rstrip("\r\n") for line in islice(source, 3))
    with evaluate_states(states, client, batch_size=2, concurrency=16, limiter=limiter) as reader:
        print(reader.schema)  # No requests yet.
        for batch in reader:
            print(batch.to_pylist())
print(limiter.attempts, limiter.target_rps, limiter.input_tokens)
```

`states.txt` has 10,000 unique, synthetic messages, one per line. Fifty authored
scenarios combine compatible products, problems, requested resolutions, timing,
context, and writing styles. They include exchanges without refunds, conditional
refunds, historical requests, policy questions, mixed intents, and routine praise.
`generate_states.py` reproduces the file with seed `20260928`, without API calls.
`states_manifest.json` records the distribution; its authoring categories are not
validated labels or a claim that this matches production traffic.

The reader has four non-nullable columns: `state` (UTF-8) and three extension types:

| Column / extension | Physical storage | Answer bytes per row |
| --- | --- | ---: |
| `department` / `jev_demo.choice` | `struct<choice: uint8, confidence: float64, probabilities: fixed_size_list<float64>[4]>` | 41 |
| `urgency` / `jev_demo.score` | `struct<score: float64, confidence: float64, probabilities: fixed_size_list<float64>[3]>` | 40 |
| `refund` / `jev_demo.noul` | `float64` | 8 |

Every struct member and list element is also non-nullable. Successful batches have
no validity bitmaps; fixed-size lists need no offsets. The 89-byte answer payload
excludes the state text, schema, and Arrow's standard buffer alignment/padding.
`uint8` is Arrow's smallest native integer and represents the chosen label's index.
The probability order is fixed by the schema, even if JSON response keys arrive in
a different order.

Only shared answer data lives in serialized extension metadata, once per schema:
Choice's ordered labels and Score's ordered legend entries. Noul needs only the
format version. In Arrow IPC, each field has an `ARROW:extension:name` identifying
the answer type and `ARROW:extension:metadata` containing version 2 JSON:

```text
jev_demo.choice: {"version":2,"labels":["returns","shipping","billing","other"]}
jev_demo.score:  {"version":2,"legend":[...three ordered legend entries...]}
jev_demo.noul:   {"version":2}
```

The Score legend is part of the API's answer, so it is retained. Its entries are
indexed from zero. The writer verifies every returned legend and probability-key
set against the shared metadata. Original question definitions, prompt instructions,
Choice option descriptions, and the requested model are not stored in the schema.

Numbers use `float64` to preserve the SDK's Python floats exactly. Neither the SDK
nor the API promises a decimal scale or float32 precision, so narrowing to float32
or storing integer percentages could lose information. Scores may be fractional.
The returned choice, confidence, score, and full probability vectors are all stored
as returned; none is recomputed from the others. `.to_pylist()` reconstructs the
complete SDK answer dictionaries, including type tags and the Score legend.

The table contains the input and complete answer objects; per-request token usage,
model names, and HTTP metadata are not stored. There is no top-level schema metadata.
API failures, missing answers, or changed legends raise errors rather than
introducing nullable answer rows.

Import `arrow_results` before opening an IPC stream so PyArrow can reconstruct the
registered extensions. Consumers without those registrations still see the storage
types and serialized field metadata:

```python
import arrow_results
import pyarrow as pa

with pa.ipc.open_stream("debug_results.arrow") as reader:
    table = reader.read_all()
print(table.to_pylist())
print(table.schema.field("department").type.metadata)
```

To print the results using **only PyArrow and the saved schema metadata**, use the
standalone `read_results.py`. It does not import the SDK or other project modules,
and needs no API key. Column names, Choice labels, probability order, and Score
legends come from the file. It prints one complete result per JSON line, reading
one Arrow batch at a time:

```sh
.venv/bin/python read_results.py parallel_results.arrows --limit 3
.venv/bin/python read_results.py parallel_results.arrows > results.jsonl
```

The path defaults to `parallel_results.arrows`; omitting `--limit` prints all rows.
The script can be copied elsewhere alongside the data file; its only third-party
dependency is `pyarrow`. As in any JSON object, integer Score keys print as strings.

Run the offline precision, schema, streaming, IPC, and corpus checks with:

```sh
.venv/bin/python -m unittest -v
```

References: [SDK answer schema](https://docs.typesafe.ai/sdk/python/api/types/responses),
[Arrow extension types](https://arrow.apache.org/docs/python/extending_types.html),
[RecordBatchReader](https://arrow.apache.org/docs/python/generated/pyarrow.RecordBatchReader.html).

Tests include real SDK calls over an offline mock HTTP transport, concurrent
out-of-order completion, shared 429/529 pauses, retry limits, permanent errors,
HTTP-date retry headers, token reservations, cancellation boundaries, and a
virtual-time simulation with a lower rolling 600-RPM server quota. The precision,
extension metadata, IPC, corpus, and single-attempt cost-estimate checks also run.

Verified with TypeSafe SDK 0.7.2 and PyArrow 25.0.1.
All 30 offline tests pass. An eight-state live parallel check completed in eight
HTTP attempts, with four requests in flight at its peak and no 429/529 responses.
`parallel_debug.arrows` contains the eight results in batches of four; input order,
non-nullability, and all extension metadata were verified after reading it back.
The interrupted earlier run's `results.arrows` still contains its 1,152 saved rows.
All four saved Arrow streams were subsequently rewritten to metadata version 2
without API calls. Their answer values, states, batch boundaries, physical storage,
precision, and nullability were verified unchanged.

The original Arrow demonstration evaluated three corpus states live;
`debug_results.arrow` contains those results in batches of two and one, with 267
bytes of answer payload.

One separate live cost probe sampled line 95 and reported 473 input tokens.
For 10,000 states this estimates 4,730,000 input tokens and **$0.19866** at the
recorded rate. The probe itself cost $0.000019866. This is a sample-based estimate,
not a measurement of all 10,000 requests.
