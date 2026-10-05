# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx2[http2]==2.13.1"]
# ///
"""Run a workload through the TypeSafe API, one row per call or packed, and record answers and timing.

Every call is billed. --dry-run prints the first call and the planned size
without calling the API. Results go to results/<label>.jsonl (one line per
row, in row order) and results/<label>.json (timing, tokens, and errors).

    TYPESAFE_API_KEY=... uv run run.py messages --strategy index --batch 32 --label messages_index_b32
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from collections import Counter
from pathlib import Path

import httpx2

from packing import KEY_NAMES, POINTERS, STRATEGIES, estimate_tokens, plan, unpack
from workloads import WORKLOADS

URL = "https://api.typesafe.ai/v1/systemone"
MAX_ATTEMPTS = 8
PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000
REQUEST_OVERHEAD_TOKENS = 259  # Measured: what an empty state and one tiny question add up to, less the question.
RESULTS = Path(__file__).with_name("results")


class TooLarge(Exception):
    """TypeSafe rejected a packed call; split it and try the halves."""


def batches_in_order(rows, size):
    return [rows[start:start + size] for start in range(0, len(rows), size)]


def batches_against_type(rows, size, reference, question, minority_share=8):
    """Batches where most rows share one reference answer and a few have another.

    Rows are sorted by their reference answer to `question`, so consecutive
    rows mostly agree. Every eighth row is set aside, and each batch gets
    size/8 of those rows taken from the far side of the sorted order, where the
    answer is usually different. Returns the batches and each row's
    {"majority": label, "contrast": bool}.
    """
    label = {row: reference[row]["answers"][question]["choice"] for row in rows}
    ordered = sorted(rows, key=lambda row: (label[row], row))
    outsiders = ordered[::minority_share]
    insiders = [row for i, row in enumerate(ordered) if i % minority_share]
    outsiders = outsiders[len(outsiders) // 2:] + outsiders[:len(outsiders) // 2]
    take_in = size - size // minority_share
    take_out = size // minority_share
    rng = random.Random(0)
    batches, notes = [], {}
    while insiders or outsiders:
        batch_in, insiders = insiders[:take_in], insiders[take_in:]
        batch_out, outsiders = outsiders[:take_out], outsiders[take_out:]
        batch = batch_in + batch_out
        counts = Counter(label[row] for row in batch)
        majority = counts.most_common(1)[0][0]
        rng.shuffle(batch)
        batches.append(batch)
        for row in batch:
            notes[row] = {"majority": majority, "contrast": label[row] != majority}
    return batches, notes


def pack_options(args):
    return {"lift_state": args.lift_state, "lift_question_first": args.lift_question_first,
            "lift_point": args.lift_point, "key_names": args.key_names}


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] if ordered else None


def retry_delay(response, attempt):
    if response is not None:
        if (ms := response.headers.get("retry-after-ms")) is not None:
            return float(ms) / 1000
        if (seconds := response.headers.get("retry-after")) is not None:
            try:
                return float(seconds)
            except ValueError:
                pass
    return min(5.0, 0.5 * 2 ** (attempt - 1)) * random.uniform(0.75, 1.25)


async def send(client, packed, outcomes):
    """POST one packed call, retrying as the TypeSafe SDK does. Returns (body, latency_ms, attempts)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.perf_counter()
        response = None
        try:
            response = await client.post(URL, json=packed.body)
        except httpx2.TransportError as error:
            outcomes[type(error).__name__] += 1
        else:
            latency = (time.perf_counter() - started) * 1000
            outcomes[str(response.status_code)] += 1
            if response.status_code == 200:
                return response.json(), latency, attempt, response.http_version
            if response.status_code in (400, 413, 422) and len(packed.rows) > 1:
                raise TooLarge(response.text[:500])
            if response.status_code not in (408, 429) and response.status_code < 500:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        if attempt == MAX_ATTEMPTS:
            raise RuntimeError(f"gave up after {attempt} attempts")
        await asyncio.sleep(retry_delay(response, attempt))


async def execute(calls, requests, args, notes):
    queue = asyncio.Queue()
    for packed in calls:
        queue.put_nowait(packed)
    outcomes, http_versions, records, rows_out = Counter(), Counter(), [], {}
    splits = [0]
    headers = {"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"}
    timeout = httpx2.Timeout(120.0)
    # TypeSafe allows 100 streams per HTTP/2 connection, and httpx2 never opens a
    # second one, so each worker uses one of several clients.
    clients = [httpx2.AsyncClient(http2=True, headers=headers, timeout=timeout) for _ in range(args.connections)]
    try:
        started = time.perf_counter()

        async def worker(client):
            while True:
                packed = await queue.get()
                try:
                    call_started = time.perf_counter() - started
                    try:
                        body, latency, attempts, version = await send(client, packed, outcomes)
                    except TooLarge:
                        splits[0] += 1
                        half = len(packed.rows) // 2
                        for rows in (packed.rows[:half], packed.rows[half:]):
                            for smaller in plan(requests, [rows], args.strategy, args.pointer, args.content_key, args.always_point, **pack_options(args)):
                                queue.put_nowait(smaller)
                        continue
                    http_versions[version] += 1
                    number = len(records)
                    records.append({
                        "call": number,
                        "rows": len(packed.rows),
                        "input_tokens": body["usage"]["input_tokens"],
                        "latency_ms": round(latency, 1),
                        "attempts": attempts,
                        "start_s": round(call_started, 3),
                        "end_s": round(time.perf_counter() - started, 3),
                    })
                    for position, (row, answer) in enumerate(zip(packed.rows, unpack(packed, body))):
                        key = {"key": packed.names[position]} if packed.names else {}
                        rows_out[row] = {"row": row, "call": number, "position": position, **key, **notes.get(row, {}), **answer}
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker(clients[n % len(clients)])) for n in range(args.concurrency)]
        joined = asyncio.create_task(queue.join())
        done, _ = await asyncio.wait([joined, *workers], return_when=asyncio.FIRST_COMPLETED)
        elapsed = time.perf_counter() - started
        for task in [joined, *workers]:
            task.cancel()
        for task in done:
            if task is not joined:
                task.result()  # A worker only finishes early by raising.
    finally:
        for client in clients:
            await client.aclose()
    return rows_out, records, outcomes, http_versions, splits[0], elapsed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workload", choices=sorted(WORKLOADS))
    parser.add_argument("--strategy", choices=STRATEGIES, default="single")
    parser.add_argument("--pointer", choices=POINTERS, default="about")
    parser.add_argument("--content-key", default="state", help="lift: the instructions field that holds the row's state")
    parser.add_argument("--always-point", action="store_true", help="index/keyed: point even when a question already names a path")
    parser.add_argument("--lift-state", default="", help="lift: the shared state")
    parser.add_argument("--lift-question-first", action="store_true", help="lift: put the question before the row's state")
    parser.add_argument("--lift-point", action="store_true", help="lift: add an inspect field naming the row's state")
    parser.add_argument("--key-names", choices=KEY_NAMES, default="numbers", help="keyed: name rows item_0, item_1, ... or after animals")
    parser.add_argument("--batch", type=int, default=32, help="rows per call")
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--order", choices=["input", "against-type"], default="input")
    parser.add_argument("--reference", type=Path, help="against-type: a one-row-per-call results file")
    parser.add_argument("--question", default="department", help="against-type: the Choice that groups rows")
    parser.add_argument("--concurrency", type=int, default=16, help="calls in flight")
    parser.add_argument("--connections", type=int, default=1, help="HTTP/2 connections to spread calls over")
    parser.add_argument("--label", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    requests = WORKLOADS[args.workload]()
    rows = list(range(args.offset, min(len(requests), args.offset + args.rows)))
    notes = {}
    if args.order == "against-type":
        reference = {record["row"]: record for record in map(json.loads, args.reference.open())}
        batches, notes = batches_against_type(rows, args.batch, reference, args.question)
    else:
        batches = batches_in_order(rows, 1 if args.strategy == "single" else args.batch)
    calls = plan(requests, batches, args.strategy, args.pointer, args.content_key, args.always_point, **pack_options(args))
    estimate = sum(estimate_tokens(call.body) + REQUEST_OVERHEAD_TOKENS for call in calls)
    print(f"{len(rows)} rows in {len(calls)} calls, about {estimate:,.0f} input tokens (${estimate * PRICE_PER_INPUT_TOKEN:.2f})")
    if args.dry_run:
        first = calls[0].body
        sample = dict(first, questions=dict(list(first["questions"].items())[:4]))
        print(json.dumps(sample, indent=2, ensure_ascii=False)[:6000])
        return

    rows_out, records, outcomes, http_versions, splits, elapsed = asyncio.run(execute(calls, requests, args, notes))
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / f"{args.label}.jsonl").open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(rows_out[row], ensure_ascii=False) + "\n")
    tokens = sum(record["input_tokens"] for record in records)
    latencies = [record["latency_ms"] for record in records]
    summary = {
        "label": args.label,
        "workload": args.workload,
        "strategy": args.strategy,
        "pointer": args.pointer if args.strategy in ("index", "keyed") else None,
        "content_key": args.content_key if args.strategy == "lift" else None,
        "always_point": args.always_point,
        **({"lift": {k: v for k, v in pack_options(args).items() if k.startswith("lift")}} if args.strategy == "lift" else {}),
        **({"key_names": args.key_names} if args.strategy == "keyed" else {}),
        "batch": 1 if args.strategy == "single" else args.batch,
        "order": args.order,
        "concurrency": args.concurrency,
        "connections": args.connections,
        "rows": len(rows),
        "calls": len(records),
        "splits": splits,
        "models": dict(Counter(row["model"] for row in rows_out.values())),
        "wall_seconds": round(elapsed, 2),
        "rows_per_second": round(len(rows) / elapsed, 1),
        "calls_per_second": round(len(records) / elapsed, 1),
        "input_tokens": tokens,
        "input_tokens_per_row": round(tokens / len(rows), 1),
        "input_tokens_per_second": round(tokens / elapsed),
        "cost_usd": round(tokens * PRICE_PER_INPUT_TOKEN, 4),
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p90": percentile(latencies, 0.9),
            "p99": percentile(latencies, 0.99),
            "max": max(latencies),
            "mean": round(statistics.fmean(latencies), 1),
        },
        "outcomes": dict(outcomes),
        "http_versions": dict(http_versions),
        "calls_detail": records,
    }
    (RESULTS / f"{args.label}.json").write_text(json.dumps(summary, indent=2) + "\n")
    brief = {key: value for key, value in summary.items() if key != "calls_detail"}
    print(json.dumps(brief, indent=2))


if __name__ == "__main__":
    main()
