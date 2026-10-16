# /// script
# requires-python = ">=3.11"
# ///
"""Print Markdown tables from multipart-vs-base64 results.

    uv run benchmarks/multipart-vs-base64/summarize.py                     # Offline sizes, and every run
    uv run benchmarks/multipart-vs-base64/summarize.py results/fiber      # One or more runs

Latency comparisons are paired: each case went through every transport, so
each difference compares the same images and questions. Medians of paired
differences come with a 95% bootstrap interval.
"""

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRANSPORTS = ("multipart", "multipart+zstd", "base64", "base64+zstd", "multipart+gzip", "base64+gzip")
BASE = "multipart"
SIZE_ORDER = {"S": 0, "M": 1, "L": 2}
KIND_ORDER = {"photo": 0, "photo_png": 1, "document": 2, "drawing": 3}
SCENARIO_ORDER = {"instructions": 0, "criteria": 1, "frames": 2, "album": 3}


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def bootstrap_median(values, rounds=2000, seed=0):
    """The median and a 95% bootstrap interval."""
    if not values:
        return None, None, None
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(rounds))
    return statistics.median(values), meds[int(rounds * 0.025)], meds[int(rounds * 0.975) - 1]


def ms(seconds):
    return "–" if seconds is None else f"{seconds * 1000:,.0f}"


def signed_ms(value, lo, hi):
    if value is None:
        return "–"
    return f"{value * 1000:+,.0f} ({lo * 1000:+,.0f} to {hi * 1000:+,.0f})"


def present(transports, records):
    return [t for t in transports if any(r["transport"] == t for r in records)]


def offline_tables():
    path = HERE / "results" / "offline.json"
    if not path.exists():
        return
    data = json.loads(path.read_text())
    rows = data["rows"]
    groups = list(dict.fromkeys(r["group"] for r in rows))
    by = {(r["group"], r["transport"]): r for r in rows}
    transports = [t for t in TRANSPORTS if any(r["transport"] == t for r in rows)]
    env = data["environment"]
    print(f"## Offline: bytes sent and client encoding time\n\n{env['platform']}, Python {env['python']}.\n")
    print("| Request | Images | Image KiB | " + " | ".join(f"{t} KiB" for t in transports) + " |")
    print("| --- " * (3 + len(transports)) + "|")
    for g in groups:
        first = by[(g, BASE)]
        cells = []
        for t in transports:
            r = by[(g, t)]
            ratio = r["wire_kib"] / first["wire_kib"]
            cells.append(f"{r['wire_kib']:,.0f} ({ratio:.2f}×){'' if r['fits'] else ' over limit'}")
        print(f"| {g} | {first['images']} | {first['image_kib']:,.0f} | " + " | ".join(cells) + " |")
    print("\n| Request | " + " | ".join(f"{t} ms" for t in transports) + " |")
    print("| --- " * (1 + len(transports)) + "|")
    for g in groups:
        print(f"| {g} | " + " | ".join(f"{by[(g, t)]['encode_ms']:,.2f}" for t in transports) + " |")
    print()


def paired(records, key):
    """Group successful single-attempt records by case and transport; key(record) labels each case's row."""
    cases = defaultdict(dict)
    for r in records:
        if r["status"] == 200 and r.get("attempts", 1) == 1:
            cases[r["case"]][r["transport"]] = r
    rows = defaultdict(list)
    for by_transport in cases.values():
        any_record = next(iter(by_transport.values()))
        rows[key(any_record)].append(by_transport)
    return rows


def latency_table(title, records, key, order):
    rows = paired(records, key)
    transports = present(TRANSPORTS, records)
    others = [t for t in transports if t != BASE]
    print(f"### {title}\n")
    print("Median latency in ms per transport; then each transport's median paired difference from multipart, "
          "with a 95% interval.\n")
    print("| Request | Cases | Image KiB | " + " | ".join(transports) + " | "
          + " | ".join(f"{t} − multipart" for t in others) + " |")
    print("| --- " * (3 + len(transports) + len(others)) + "|")
    for name in sorted(rows, key=order):
        cases = rows[name]
        image_kib = statistics.median(c[next(iter(c))]["image_bytes"] for c in cases) / 1024
        medians = [ms(statistics.median([c[t]["total_s"] for c in cases if t in c]) if any(t in c for c in cases) else None)
                   for t in transports]
        diffs = []
        for t in others:
            d = [c[t]["total_s"] - c[BASE]["total_s"] for c in cases if t in c and BASE in c]
            diffs.append(signed_ms(*bootstrap_median(d)))
        print(f"| {name} | {len(cases)} | {image_kib:,.0f} | " + " | ".join(medians) + " | " + " | ".join(diffs) + " |")
    print()
    print(f"Median TypeSafe processing time (`x-envoy-upstream-service-time`), ms, and the paired difference from multipart:\n")
    print("| Request | " + " | ".join(transports) + " | " + " | ".join(f"{t} − multipart" for t in others) + " |")
    print("| --- " * (1 + len(transports) + len(others)) + "|")
    for name in sorted(rows, key=order):
        cases = rows[name]
        medians = []
        for t in transports:
            vals = [c[t]["server_s"] for c in cases if t in c and c[t].get("server_s") is not None]
            medians.append(ms(statistics.median(vals)) if vals else "–")
        diffs = []
        for t in others:
            d = [c[t]["server_s"] - c[BASE]["server_s"] for c in cases
                 if t in c and BASE in c and c[t].get("server_s") is not None and c[BASE].get("server_s") is not None]
            diffs.append(signed_ms(*bootstrap_median(d)))
        print(f"| {name} | " + " | ".join(medians) + " | " + " | ".join(diffs) + " |")
    print()


def link_fit(records):
    """Least-squares fit of (latency − processing time) against bytes sent: fixed overhead and effective uplink."""
    pts = [(r["wire_bytes"], r["total_s"] - r["server_s"]) for r in records
           if r["status"] == 200 and r.get("attempts", 1) == 1 and r.get("server_s") is not None and r["phase"] != "throughput"]
    if len(pts) < 10:
        return None
    xs, ys = zip(*pts)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return {"overhead_s": my - slope * mx, "mbit_per_s": 8 / (slope * 1e6) if slope > 0 else float("inf"), "n": len(pts)}


def fit_line(pts):
    xs, ys = zip(*pts)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx if sxx else 0.0
    return my - slope * mx, slope


def extra_bytes_table(records):
    """Pooled over every paired case: how much latency each transport adds per byte it sends beyond multipart."""
    cases = paired([r for r in records if r["phase"] in ("sizes", "placement")], lambda r: "all").get("all", [])
    transports = [t for t in present(TRANSPORTS, records) if t != BASE]
    if len(cases) < 10:
        return
    print("### Latency against extra bytes, all paired cases\n")
    print("A line fitted through every case's (bytes sent − multipart's, latency − multipart's). The slope gives the "
          "link speed those extra bytes cost; the intercept is the difference left at equal size.\n")
    print("| Transport | Cases | Median extra KiB sent | Median extra ms | Intercept ms (95%) | Implied Mbit/s | "
          "Median extra processing ms |")
    print("| --- " * 7 + "|")
    rng = random.Random(0)
    for t in transports:
        pts = [(c[t]["wire_bytes"] - c[BASE]["wire_bytes"], c[t]["total_s"] - c[BASE]["total_s"]) for c in cases
               if t in c and BASE in c]
        if len(pts) < 10:
            continue
        intercept, slope = fit_line(pts)
        boots = sorted(fit_line(rng.choices(pts, k=len(pts)))[0] for _ in range(1000))
        server = [c[t]["server_s"] - c[BASE]["server_s"] for c in cases
                  if t in c and BASE in c and c[t].get("server_s") is not None and c[BASE].get("server_s") is not None]
        spread = max(x for x, _ in pts) - min(x for x, _ in pts)
        speed = f"{8 / (slope * 1e6):,.1f}" if slope > 0 and spread > 50_000 else "–"
        print(f"| {t} | {len(pts)} | {statistics.median(x for x, _ in pts) / 1024:+,.0f} | "
              f"{statistics.median(y for _, y in pts) * 1000:+,.0f} | {intercept * 1000:+,.0f} "
              f"({boots[25] * 1000:+,.0f} to {boots[974] * 1000:+,.0f}) | {speed} | "
              f"{statistics.median(server) * 1000 if server else 0:+,.1f} |")
    print()


def throughput_table(blocks):
    if not blocks:
        return
    print("### Throughput\n")
    print("Each transport ran two blocks (A B C D D C B A); values are the mean of its blocks. Requests in a block "
          "were sent all at once, up to the concurrency.\n")
    print("| Images | Concurrency | Requests per block | Transport | Images/s | vs multipart | Mbit/s sent | "
          "p50 latency ms | Retries |")
    print("| --- " * 9 + "|")
    groups = defaultdict(lambda: defaultdict(list))
    for b in blocks:
        groups[(b["kind"], b["size"], b["concurrency"])][b["transport"]].append(b)
    for (kind, size, concurrency), by_t in sorted(groups.items(), key=lambda kv: (KIND_ORDER.get(kv[0][0], 9), kv[0][1], kv[0][2])):
        base = statistics.fmean(b["images_per_s"] for b in by_t.get(BASE, [])) if BASE in by_t else None
        for t in [t for t in TRANSPORTS if t in by_t]:
            bs = by_t[t]
            rate = statistics.fmean(b["images_per_s"] for b in bs)
            print(f"| {kind} {size} | {concurrency} | {bs[0]['requests']} | {t} | {rate:,.1f} | "
                  f"{rate / base:.2f}× | {statistics.fmean(b['wire_mbit_per_s'] for b in bs):,.1f} | "
                  f"{statistics.fmean(b['latency_p50_s'] for b in bs) * 1000:,.0f} | {sum(b['retried'] for b in bs)} |")
    print()


def limits_table(records):
    rows = [r for r in records if r["phase"] == "limits"]
    if not rows:
        return
    print("### A request near the 32 MiB limit\n")
    print("| Request | Transport | Images | Body MiB | Sent MiB | Status | Attempts | Latency ms | Error |")
    print("| --- " * 9 + "|")
    for r in rows:
        error = (r.get("error") or "").replace("|", "/")[:120]
        print(f"| {r['case']}{' (long context)' if r.get('long_context') else ''} | {r['transport']} | {r['images']} | "
              f"{r['body_bytes'] / 2**20:,.1f} | {r['wire_bytes'] / 2**20:,.1f} | {r['status']} | {r.get('attempts', 1)} | "
              f"{ms(r['total_s'])} | {error} |")
    print()


def iou(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, right - left) * max(0.0, bottom - top)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def agreement_table(records):
    """Do the transports get the same tokens and answers for the same request?"""
    cases = defaultdict(dict)
    for r in records:
        if r["status"] == 200 and r["phase"] in ("sizes", "placement", "limits"):
            cases[r["case"]][r["transport"]] = r
    transports = [t for t in present(TRANSPORTS, records) if t != BASE]
    if not cases or not transports:
        return
    print("### Same request, same answers?\n")
    print("Each transport compared with multipart on the same case.\n")
    print("| Transport | Cases | Same input tokens | Same Choice | Largest Noul difference | Median Box edge difference | "
          "Largest Box edge difference |")
    print("| --- " * 7 + "|")
    for t in transports:
        n = same_tokens = same_choice = choices = 0
        noul, edges = [], []
        for by_t in cases.values():
            if t not in by_t or BASE not in by_t:
                continue
            a, b = by_t[BASE], by_t[t]
            n += 1
            same_tokens += a.get("input_tokens") == b.get("input_tokens")
            for qid, ans in a.get("answers", {}).items():
                other = b.get("answers", {}).get(qid)
                if isinstance(ans, float):
                    noul.append(abs(ans - other))
                elif "choice" in ans:
                    choices += 1
                    same_choice += ans["choice"] == other["choice"]
                elif "box" in ans:
                    edges += [abs(x - y) for x, y in zip(ans["box"], other["box"])]
        print(f"| {t} | {n} | {same_tokens}/{n} | {same_choice}/{choices} | {max(noul, default=0):.4f} | "
              f"{statistics.median(edges) if edges else 0:.4f} | {max(edges, default=0):.4f} |")
    print()


def accuracy_table(records):
    """Answers against the drawn truth, per transport: a check that every transport sends the same images."""
    stats = defaultdict(lambda: defaultdict(list))
    for r in records:
        if r["status"] != 200 or "truth" not in r or r["phase"] not in ("sizes", "placement"):
            continue
        s = stats[r["transport"]]
        for qid, truth in r["truth"].items():
            ans = r["answers"].get(qid)
            if ans is None:
                continue
            if isinstance(truth, bool):
                s["yes/no correct"].append((ans > 0.5) == truth)
            elif qid == "coverage":
                s["coverage level error"].append(abs(ans["score"] - truth))
            elif isinstance(truth, str):
                s["choice correct"].append(ans["choice"] == truth)
            elif isinstance(truth, list):
                s["box IoU"].append(iou(ans["box"], truth))
    if not stats:
        return
    print("### Accuracy against the drawn targets\n")
    print("| Transport | Yes/no correct | Choice correct | Median coverage level error | Median box IoU |")
    print("| --- " * 5 + "|")
    for t in [t for t in TRANSPORTS if t in stats]:
        s = stats[t]
        fmt = lambda k, f: f(s[k]) if s[k] else "–"
        print(f"| {t} | {fmt('yes/no correct', lambda v: f'{sum(v) / len(v):.0%} of {len(v)}')} | "
              f"{fmt('choice correct', lambda v: f'{sum(v) / len(v):.0%} of {len(v)}')} | "
              f"{fmt('coverage level error', lambda v: f'{statistics.median(v):.2f}')} | "
              f"{fmt('box IoU', lambda v: f'{statistics.median(v):.2f} of {len(v)}')} |")
    print()


def order_table(records):
    """Processing time by position within each case's rotation: a cache would make later positions faster."""
    by_pos = defaultdict(list)
    for r in records:
        if r["status"] == 200 and r.get("position") is not None and r.get("server_s") is not None:
            by_pos[r["position"]].append(r["server_s"])
    if len(by_pos) < 2:
        return
    print("Median TypeSafe processing time by position in each case's rotation (a cache would make later positions "
          "faster): " + ", ".join(f"{p + 1}: {ms(statistics.median(v))} ms" for p, v in sorted(by_pos.items())) + ".\n")


def run_tables(run_dir):
    records = [r for r in load_jsonl(run_dir / "requests.jsonl") if r["phase"] != "warmup"]
    runs = load_jsonl(run_dir / "runs.jsonl")
    blocks = load_jsonl(run_dir / "throughput.jsonl")
    if not records:
        return
    env = runs[0] if runs else {}
    tokens = sum(r.get("input_tokens") or 0 for r in records)
    statuses = defaultdict(int)
    for r in records:
        statuses[r["status"]] += 1
    colos = sorted({r["colo"] for r in records if r.get("colo")})
    models = sorted({r["model"] for r in records if r.get("model")})
    print(f"## Run: {run_dir.name}\n")
    print(f"- Started {env.get('started')}; {env.get('platform')}; Python {env.get('python')}; "
          f"{'HTTP/2' if env.get('http2') else 'HTTP/1.1'}")
    print(f"- Network: {env.get('network')}" + (f"; networkQuality {env['network_quality']}" if env.get("network_quality") else ""))
    print(f"- Model {', '.join(models)}; Cloudflare location {', '.join(colos)}")
    print(f"- {len(records):,} requests, statuses {dict(statuses)}; {tokens:,} input tokens (${tokens * 0.042e-6:.2f})")
    fit = link_fit(records)
    if fit:
        print(f"- Latency minus processing time fits {fit['overhead_s'] * 1000:,.0f} ms + bytes sent at "
              f"{fit['mbit_per_s']:,.1f} Mbit/s ({fit['n']} sequential requests)")
    print()
    sizes = [r for r in records if r["phase"] == "sizes"]
    if sizes:
        latency_table("One image, four questions, by image kind and size", sizes,
                      lambda r: f"{r['kind']} {r['size']}", lambda name: (KIND_ORDER.get(name.split()[0], 9), SIZE_ORDER[name.split()[1]]))
    placement = [r for r in records if r["phase"] == "placement"]
    if placement:
        latency_table("Images in instructions, criteria, frames, and albums", placement,
                      lambda r: f"{r['scenario']} ({r['images']} images)", lambda name: SCENARIO_ORDER.get(name.split()[0], 9))
    order_table([r for r in records if r["phase"] in ("sizes", "placement")])
    extra_bytes_table(records)
    throughput_table(blocks)
    limits_table(records)
    agreement_table(records)
    accuracy_table(records)


def request_name(r):
    return f"{r['kind']} {r['size']}" if r["phase"] == "sizes" else f"{r['scenario']} ({r['images']} images)"


def request_order(name):
    head = name.split()[0]
    if head in KIND_ORDER:
        return (0, KIND_ORDER[head], SIZE_ORDER[name.split()[1]])
    return (1, SCENARIO_ORDER.get(head, 9), 0)


def compare_tables(run_dirs):
    """Side by side: each run's paired difference from multipart, as a share of multipart's median latency."""
    runs = {d.name: [r for r in load_jsonl(d / "requests.jsonl") if r["phase"] in ("sizes", "placement")] for d in run_dirs}
    names = sorted({request_name(r) for records in runs.values() for r in records}, key=request_order)
    for t in ("base64", "base64+zstd", "multipart+zstd"):
        print(f"## Compared: {t} − multipart\n")
        print("Median paired difference in ms, and as a share of multipart's median latency.\n")
        print("| Request | " + " | ".join(runs) + " |")
        print("| --- " * (1 + len(runs)) + "|")
        for name in names:
            cells = []
            for records in runs.values():
                cases = paired([r for r in records if request_name(r) == name], request_name).get(name, [])
                d = [c[t]["total_s"] - c[BASE]["total_s"] for c in cases if t in c and BASE in c]
                if not d:
                    cells.append("–")
                    continue
                base = statistics.median(c[BASE]["total_s"] for c in cases if BASE in c)
                med = statistics.median(d)
                cells.append(f"{med * 1000:+,.0f} ms ({med / base:+.0%})")
            print(f"| {name} | " + " | ".join(cells) + " |")
        print()
    blocks = {d.name: load_jsonl(d / "throughput.jsonl") for d in run_dirs}
    keys = sorted({(b["kind"], b["size"], b["concurrency"]) for bs in blocks.values() for b in bs},
                  key=lambda k: (KIND_ORDER.get(k[0], 9), k[1], k[2]))
    if keys:
        print("## Compared: throughput relative to multipart\n")
        print("| Images | Concurrency | Transport | " + " | ".join(blocks) + " |")
        print("| --- " * (3 + len(blocks)) + "|")
        for kind, size, concurrency in keys:
            for t in [t for t in TRANSPORTS if t != BASE]:
                cells = []
                for bs in blocks.values():
                    mine = [b["images_per_s"] for b in bs if (b["kind"], b["size"], b["concurrency"], b["transport"]) == (kind, size, concurrency, t)]
                    base = [b["images_per_s"] for b in bs if (b["kind"], b["size"], b["concurrency"], b["transport"]) == (kind, size, concurrency, BASE)]
                    cells.append(f"{statistics.fmean(mine) / statistics.fmean(base):.2f}× ({statistics.fmean(mine):,.0f}/s)"
                                 if mine and base else "–")
                if any(c != "–" for c in cells):
                    print(f"| {kind} {size} | {concurrency} | {t} | " + " | ".join(cells) + " |")
        print()


def main():
    args = [Path(a) for a in sys.argv[1:]]
    if not args:
        offline_tables()
        args = sorted(p for p in (HERE / "results").glob("*") if p.is_dir() and (p / "requests.jsonl").exists())
    for run_dir in args:
        run_tables(run_dir)
    if len(args) > 1:
        compare_tables(args)


if __name__ == "__main__":
    main()
