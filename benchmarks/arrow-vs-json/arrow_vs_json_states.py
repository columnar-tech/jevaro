# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow==25.0.1", "orjson==3.12.0"]
# ///
"""How much time could sending states as Arrow IPC save, compared with JSON?

This measures only data handling, not end-to-end inference. States start as an
Arrow table, as they would after a database query or in a dataframe. Each
approach is timed on the client (encoding the request) and on a hypothetical
server that accepts it (decoding the request into a usable form):

- Per-state JSON: today's API shape. One body per state, each repeating the
  questions and model, built from Python objects and parsed back into them.
- Bulk JSON: a hypothetical JSON batch endpoint. One body with a states array.
- Arrow IPC: one IPC stream of the table, plus the questions as JSON once. The
  server reads the stream straight into columns.

It also times a server that reads Arrow but then needs Python objects per
state, which bounds the saving if only the serving layer is Arrow-native.

Run with: uv run benchmarks/arrow-vs-json/arrow_vs_json_states.py [rows]
"""

import json
import random
import statistics
import sys
import time

import orjson
import pyarrow as pa
import pyarrow.compute as pc

ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
REPEATS = 9
MODEL = "jev-latest"
QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which team should handle this customer message?",
        "criteria": {"returns": "Returns, exchanges, refunds", "shipping": "Delivery problems",
                     "billing": "Charges and payments", "other": "None of these teams fits"},
    },
    "urgency": {
        "type": "score",
        "instructions": "How soon does the customer want a resolution?",
        "criteria": ["No deadline or can wait", "Within the next few days", "Today or immediately"],
    },
    "refund": {"type": "noul", "instructions": "Is the customer requesting a refund?"},
}
COLUMN_SETS = {
    "1 column (body)": ["body"],
    "3 columns": ["id", "subject", "body"],
    "8 columns": ["id", "subject", "body", "customer_tier", "order_total", "item_count",
                  "created_at", "tags"],
}


def make_tickets(rows):
    """Synthetic support tickets; message bodies are 75 to 330 characters."""
    rng = random.Random(20261005)
    words = ["".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=rng.randint(2, 9))) for _ in range(3000)]

    def text(low, high):
        target, out = rng.randint(low, high), []
        while sum(len(w) + 1 for w in out) < target:
            out.append(rng.choice(words))
        return " ".join(out)[:high].capitalize() + "."

    tags = ["damaged", "late", "wrong-item", "refund", "invoice", "gift", "repeat-customer"]
    return pa.table({
        "id": pa.array(range(100_000, 100_000 + rows), pa.int64()),
        "subject": [text(15, 60) for _ in range(rows)],
        "body": [text(75, 330) for _ in range(rows)],
        "customer_tier": pa.array([rng.choice(["free", "pro", "team", "enterprise"]) for _ in range(rows)]).dictionary_encode(),
        "order_total": pa.array([round(rng.uniform(5, 2000), 2) for _ in range(rows)], pa.float64()),
        "item_count": pa.array([rng.randint(1, 20) for _ in range(rows)], pa.int32()),
        "created_at": pa.array([1_790_000_000_000 + rng.randint(0, 10**9) for _ in range(rows)], pa.timestamp("ms", "UTC")),
        "tags": pa.array([rng.sample(tags, rng.randint(0, 3)) for _ in range(rows)], pa.list_(pa.string())),
    })


def json_ready(table):
    """What a JSON client must do first: JSON has no timestamp type, so make strings, then make rows."""
    if "created_at" in table.column_names:
        index = table.column_names.index("created_at")
        iso = pc.strftime(table["created_at"], format="%Y-%m-%dT%H:%M:%SZ")
        table = table.set_column(index, "created_at", iso)
    if table.num_columns == 1:
        return table.column(0).to_pylist()  # A single column's values are the states.
    return table.to_pylist()


def per_state_stdlib(table):
    states = json_ready(table)
    return [json.dumps({"model": MODEL, "state": s, "questions": QUESTIONS}).encode() for s in states]


def per_state_orjson(table):
    states = json_ready(table)
    return [orjson.dumps({"model": MODEL, "state": s, "questions": QUESTIONS}) for s in states]


def bulk_orjson(table):
    return [orjson.dumps({"model": MODEL, "states": json_ready(table), "questions": QUESTIONS})]


def arrow_ipc(table):
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    request = {"model": MODEL, "questions": QUESTIONS}
    if table.num_columns == 1:
        request["state_column"] = table.column_names[0]
    return [orjson.dumps(request), sink.getvalue()]


def decode_stdlib(bodies):
    return [json.loads(b) for b in bodies]


def decode_orjson(bodies):
    return [orjson.loads(b) for b in bodies]


def decode_arrow(parts):
    request = orjson.loads(parts[0])
    return request, pa.ipc.open_stream(parts[1]).read_all()


def decode_arrow_then_rows(parts):
    request, table = decode_arrow(parts)
    if "state_column" in request:
        return table.column(request["state_column"]).to_pylist()
    return table.to_pylist()


APPROACHES = [
    ("Per-state JSON (stdlib json, today's SDK)", per_state_stdlib, decode_stdlib),
    ("Per-state JSON (orjson)", per_state_orjson, decode_orjson),
    ("Bulk JSON (orjson)", bulk_orjson, decode_orjson),
    ("Arrow IPC", arrow_ipc, decode_arrow),
    ("Arrow IPC, server then needs Python rows", arrow_ipc, decode_arrow_then_rows),
]


def median_ms(fn, *args):
    times = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        result = fn(*args)
        times.append(time.perf_counter() - start)
    return statistics.median(times) * 1000, result


def main():
    tickets = make_tickets(ROWS)
    print(f"{ROWS:,} states; median of {REPEATS} runs; Python {sys.version.split()[0]}, "
          f"pyarrow {pa.__version__}, orjson {orjson.__version__}\n")
    for label, columns in COLUMN_SETS.items():
        table = tickets.select(columns)
        print(f"{label}: {', '.join(f'{f.name}: {f.type}' for f in table.schema)}")
        print(f"  {'Approach':<43} {'Client':>9} {'Server':>9} {'Total':>9} {'Payload':>9} {'vs Arrow':>9}")
        rows = []
        for name, encode, decode in APPROACHES:
            client, payload = median_ms(encode, table)
            server, _ = median_ms(decode, payload)
            size = sum(len(p) for p in payload) / 1e6
            rows.append((name, client, server, client + server, size))
        arrow_total = rows[3][3]
        for name, client, server, total, size in rows:
            print(f"  {name:<43} {client:7.2f}ms {server:7.2f}ms {total:7.2f}ms {size:7.2f}MB {total / arrow_total:8.0f}x")
        print()


if __name__ == "__main__":
    main()
