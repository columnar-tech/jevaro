"""How many states per second can Jevaro stream with rows_per_call?

Runs a Jevaro server in its own process and streams states through it with the
Python SDK. Upstream is the TypeSafe API (paid) or a local fake (free) whose
latency grows with the number of questions in a call. The server process
counts upstream calls, statuses, input tokens, and calls in flight.

    .venv/bin/python benchmarks/rows-per-call/bench.py --rows 10000 --rows-per-call 32 --concurrency 64 --fake
    TYPESAFE_API_KEY=... .venv/bin/python benchmarks/rows-per-call/bench.py --rows 100000 --rows-per-call 64 --concurrency 256

Results go to benchmarks/rows-per-call/results/<label>.json (gitignored).
"""

import argparse
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000
QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which team should handle this customer message?",
        "criteria": {
            "returns": "Return, exchange, or refund requests",
            "shipping": "Delivery status or missing packages",
            "billing": "Incorrect charges, invoices, or payment errors",
            "other": "None of these teams fits the request",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How soon does the customer want a resolution?",
        "criteria": [
            "Customer says the issue can wait or gives no deadline.",
            "Customer asks for resolution within the next few days.",
            "Customer asks for resolution today or immediately.",
        ],
    },
    "refund": {"type": "noul", "instructions": "Is the customer requesting a refund?"},
}


def states(count):
    """Distinct synthetic customer messages: the benchmark corpus, then more from other seeds."""
    sys.path.insert(0, str(ROOT / "experiments/state-packing"))
    import corpus

    out, seen, seed = [], set(), corpus.SEED
    while len(out) < count:
        for text, *_ in corpus.generate(seed):
            if text not in seen:
                seen.add(text)
                out.append(text)
        seed += 1
    return out[:count]


def workload(name, count):
    """(states, questions): plain messages with 3 questions, or structured tickets with 6."""
    if name == "messages":
        return states(count), QUESTIONS
    sys.path.insert(0, str(ROOT / "experiments/state-packing"))
    import workloads

    requests = workloads.tickets()
    if count > len(requests):
        raise SystemExit(f"The tickets workload has {len(requests)} states")
    return [request.state for request in requests[:count]], workloads.TICKET_QUESTIONS


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_port(port, process, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("A benchmark process did not start")
            time.sleep(0.05)


def serve(port, stats_path, concurrency, connections, verify):
    """Server process: Jevaro with a transport that counts upstream calls."""
    import asyncio

    import httpx2
    import uvicorn

    from jevaro_server.app import create_app

    stats = {"statuses": Counter(), "latencies": [], "tokens": 0, "active": 0, "peak": 0, "area": 0.0, "timeline": []}
    clock = {"last": None, "first": None}

    def tick():
        now = time.monotonic()
        if clock["last"] is not None:
            stats["area"] += stats["active"] * (now - clock["last"])
        else:
            clock["first"] = now
        clock["last"] = now

    class Counting(httpx2.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            tick()
            stats["active"] += 1
            stats["peak"] = max(stats["peak"], stats["active"])
            started = time.monotonic()
            try:
                response = await super().handle_async_request(request)
                body = await response.aread()
            except Exception as error:
                stats["statuses"][type(error).__name__] += 1
                raise
            finally:
                tick()
                stats["active"] -= 1
            stats["statuses"][str(response.status_code)] += 1
            stats["latencies"].append((time.monotonic() - started) * 1000)
            tokens = json.loads(body)["usage"]["input_tokens"] if response.status_code == 200 else 0
            stats["tokens"] += tokens
            stats["timeline"].append((round(started, 3), round(time.monotonic(), 3), response.status_code, tokens))
            return response

    def transport():
        return Counting(http2=True, verify=verify) if verify else Counting(http2=True)

    app = create_app(transport_factory=transport, concurrency=concurrency, connections=connections)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))

    def write_stats(*_):
        times = os.times()
        span = (clock["last"] - clock["first"]) if clock["first"] is not None else 0
        Path(stats_path).write_text(json.dumps({
            "cpu_seconds": times.user + times.system,
            "statuses": stats["statuses"], "latencies": stats["latencies"], "tokens": stats["tokens"],
            "peak_in_flight": stats["peak"], "mean_in_flight": stats["area"] / span if span else 0,
            "timeline": stats["timeline"],
        }))
        server.should_exit = True

    signal.signal(signal.SIGTERM, write_stats)
    if profile := os.environ.get("BENCH_PROFILE"):  # Write cProfile stats for the server process.
        import cProfile

        with cProfile.Profile() as profiler:
            asyncio.run(server.serve())
        profiler.dump_stats(profile)
    else:
        asyncio.run(server.serve())


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 1) if ordered else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--workload", choices=["messages", "tickets"], default="messages")
    parser.add_argument("--rows-per-call", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=256, help="JEVARO_CONCURRENCY")
    parser.add_argument("--connections", type=int, default=1, help="JEVARO_UPSTREAM_CONNECTIONS")
    parser.add_argument("--fake", action="store_true", help="use the local fake instead of the TypeSafe API")
    parser.add_argument("--fake-latency", default="165,0.74,0.3", help="BASE_MS,PER_QUESTION_MS,SPREAD")
    parser.add_argument("--label")
    parser.add_argument("--serve", nargs=5, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        port, stats_path, concurrency, connections, verify = args.serve
        return serve(int(port), stats_path, int(concurrency), int(connections), verify or None)

    label = args.label or (f"{'fake' if args.fake else 'live'}_{args.workload}_r{args.rows}_b{args.rows_per_call}"
                           f"_c{args.concurrency}_x{args.connections}")
    data, questions = workload(args.workload, args.rows)
    processes = []
    with tempfile.TemporaryDirectory(prefix="jevaro-bench-") as scratch:
        env = dict(os.environ)
        verify = ""
        if args.fake:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-keyout", f"{scratch}/key.pem", "-out", f"{scratch}/cert.pem", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           check=True, capture_output=True)
            fake_port = free_port()
            fake = subprocess.Popen(["node", str(HERE / "fake_typesafe.cjs"), str(fake_port), scratch,
                                     *args.fake_latency.split(",")], stdout=subprocess.DEVNULL)
            processes.append(fake)
            wait_for_port(fake_port, fake)
            env.update(TYPESAFE_UPSTREAM_URL=f"https://localhost:{fake_port}", TYPESAFE_API_KEY="fake-key")
            verify = f"{scratch}/cert.pem"
        elif not env.get("TYPESAFE_API_KEY"):
            raise SystemExit("Set TYPESAFE_API_KEY, or pass --fake")
        port, stats_path = free_port(), f"{scratch}/server.json"
        server = subprocess.Popen([sys.executable, __file__, "--serve", str(port), stats_path,
                                   str(args.concurrency), str(args.connections), verify], env=env)
        processes.append(server)
        try:
            wait_for_port(port, server)
            from jevaro import TypeSafeClient

            started = time.monotonic()
            client_cpu = time.process_time()
            sizes, first_answer = [], None
            error = None
            with TypeSafeClient(api_key=env["TYPESAFE_API_KEY"], base_url=f"http://127.0.0.1:{port}",
                                timeout=600) as client:
                try:
                    with client.system_one(states=data, questions=questions,
                                           rows_per_call=args.rows_per_call) as reader:
                        for batch in reader:
                            if batch.num_rows and first_answer is None:
                                first_answer = time.monotonic() - started
                            sizes.append(batch.num_rows)
                except Exception as caught:  # Report what streamed before a failure.
                    error = repr(caught)
            elapsed = time.monotonic() - started
            client_cpu = time.process_time() - client_cpu
        finally:
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=60)
            for process in processes[:-1]:
                process.terminate()
                process.wait(timeout=10)
        served = json.loads(Path(stats_path).read_text())

    rows = sum(sizes)
    latencies = served["latencies"]
    report = {
        "label": label,
        "upstream": "fake" if args.fake else "typesafe",
        "workload": args.workload,
        "rows_requested": args.rows,
        "rows": rows,
        "error": error,
        "rows_per_call": args.rows_per_call,
        "concurrency": args.concurrency,
        "connections": args.connections,
        "wall_seconds": round(elapsed, 2),
        "rows_per_second": round(rows / elapsed, 1),
        "first_answer_seconds": round(first_answer, 2) if first_answer is not None else None,
        "upstream_calls": sum(served["statuses"].values()),
        "upstream_statuses": served["statuses"],
        "upstream_latency_ms": {"p50": percentile(latencies, 0.5), "p90": percentile(latencies, 0.9),
                                "p99": percentile(latencies, 0.99),
                                "mean": round(statistics.fmean(latencies), 1) if latencies else None},
        "calls_in_flight": {"peak": served["peak_in_flight"], "mean": round(served["mean_in_flight"], 1)},
        "input_tokens": served["tokens"],
        "input_tokens_per_row": round(served["tokens"] / rows, 1) if rows else None,
        "input_tokens_per_second": round(served["tokens"] / elapsed),
        "cost_usd": round(served["tokens"] * PRICE_PER_INPUT_TOKEN, 4) if not args.fake else 0,
        "server_cpu_share": round(served["cpu_seconds"] / elapsed, 2),
        "client_cpu_share": round(client_cpu / elapsed, 2),
        "batches": len(sizes),
        "timeline": served["timeline"],  # (start, end, status, input tokens) per upstream attempt
    }
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / f"{label}.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
