# Arrow IPC vs JSON for bulk states

How much data handling could a bulk API save by accepting states as Arrow IPC
instead of JSON? This benchmark times only a client encoding a request and a
server decoding it, for 10,000 synthetic support-ticket states that start as
an Arrow table, as they would after a database query or in a dataframe.
It does not include inference, network transfer, or HTTP overhead.

```sh
uv run benchmarks/arrow-vs-json/arrow_vs_json_states.py         # 10,000 states
uv run benchmarks/arrow-vs-json/arrow_vs_json_states.py 100000  # Another count
```

## Approaches

| Approach | Client | Server |
| --- | --- | --- |
| Per-state JSON | One JSON body per state, each repeating the questions | Parses each body into Python objects |
| Bulk JSON | One JSON body with a `states` array | Parses it into Python objects |
| Arrow IPC | One Arrow IPC stream of the table, plus the questions as JSON once | Reads the stream into columns without copying |
| Arrow IPC, then Python rows | As above | Then builds one Python object per state |

Both JSON approaches must first turn the table's columns into one object per
state. Per-state JSON is timed with the standard library's `json`, which the
TypeSafe Python SDK uses, and with orjson. Bulk JSON uses orjson.

The 1-column states are plain strings (`body`). The 3-column states have `id`,
`subject`, and `body`. The 8-column states add a dictionary-encoded tier, a
float, an integer, a timestamp, and a list of strings.

## Results

Apple M3 with 16 GB, macOS 26.6, Python 3.14.0, pyarrow 25.0.1, orjson 3.12.0.
Client plus server time for 10,000 states, median of 9 runs:

| Approach | 1 column | 3 columns | 8 columns |
| --- | --- | --- | --- |
| Per-state JSON, `json` | 63.2 ms | 69.5 ms | 98.9 ms |
| Per-state JSON, orjson | 20.9 ms | 23.1 ms | 40.5 ms |
| Bulk JSON, orjson | 1.44 ms | 4.80 ms | 23.4 ms |
| **Arrow IPC** | **0.05 ms** | **0.08 ms** | **0.09 ms** |
| Arrow IPC, then Python rows | 0.53 ms | 2.02 ms | 23.3 ms |

| Payload | 1 column | 3 columns | 8 columns |
| --- | --- | --- | --- |
| Per-state JSON, `json` | 7.9 MB | 8.7 MB | 10.1 MB |
| Bulk JSON | 2.1 MB | 2.8 MB | 4.1 MB |
| Arrow IPC | 2.1 MB | 2.6 MB | 3.1 MB |

## What it shows

- Arrow IPC handled the states 29× to 253× faster than one bulk JSON body, and
  923× to 1,278× faster than per-state JSON with `json`: two to three orders
  of magnitude.
- The gap grows as states get wider. Most of the JSON time goes into turning
  columns into one object per state and back again; Arrow skips both steps.
- The gain depends on columns reaching the model's input processing. A server
  that rebuilds a Python object for each state from the Arrow data gives back
  nearly all of the gain for 8-column states (23.3 ms against 23.4 ms).

## Caveats

- Ratios varied by up to about 15% between runs on this machine.
- Both sides are Python. A server written in Rust or C++ would decode JSON
  faster, but the client's conversion to objects and Arrow's zero-copy read
  would not change.
- These are milliseconds per 10,000 states, while inference over the same
  states takes seconds today. The saving matters most for a native bulk path.
- The text is synthetic, with message bodies of 75 to 330 characters.
