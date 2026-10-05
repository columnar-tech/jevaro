"""Print a table of benchmark results: python summarize.py [results/*.json]"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    paths = [Path(p) for p in sys.argv[1:]] or sorted((HERE / "results").glob("*.json"), key=lambda p: p.stat().st_mtime)
    print("| Run | Rows | Rows per call | Concurrency | Connections | Rows/s | Calls | p50 latency | Calls in flight | "
          "Tokens per row | Tokens/s | Server CPU | Cost | Non-200 |")
    print("| --- " * 14 + "|")
    for path in paths:
        r = json.loads(path.read_text())
        other = {status: n for status, n in r["upstream_statuses"].items() if status != "200"}
        print(f"| {r['label']} | {r['rows']:,} | {r['rows_per_call']} | {r['concurrency']} | {r.get('connections', 1)} | "
              f"{r['rows_per_second']:,.0f} | {r['upstream_calls']:,} | {r['upstream_latency_ms']['p50']} ms | "
              f"{r['calls_in_flight']['mean']} (peak {r['calls_in_flight']['peak']}) | {r['input_tokens_per_row']} | "
              f"{r['input_tokens_per_second']:,} | {r['server_cpu_share']:.0%} | ${r['cost_usd']:.2f} | "
              f"{other or ''}{' ' + r['error'] if r['error'] else ''} |")


if __name__ == "__main__":
    main()
