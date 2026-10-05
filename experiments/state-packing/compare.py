"""Compare packed answers with one-row-per-call answers, and tabulate speed and cost.

    python compare.py results/messages_single.jsonl results/messages_index_b32.jsonl ...

The first file is the reference. For each other file, and each question, it
reports how often the answers agree and how far apart their probabilities are:

- Choice: top choice agreement, and mean total variation distance (TVD)
  between the two probability distributions.
- Score: mean absolute difference in score, and mean TVD over the levels.
- Noul: mean absolute difference in probability, and agreement at 0.5.

It also breaks agreement down by a row's position in its packed call, by how
confident the reference was (for Choice questions), and, for runs batched
against type, by whether a row's answer differed from most of its call. With
--totals it prints each run's answers summed over all rows: the share of each
Choice option, the mean Score, and the share of Nouls at 0.5 or above.

With --labels it also scores department and urgency against how each message
was written: its scenario group (returns, shipping, billing, or other; mixed
scenarios are skipped) and its deadline style (today is level 2, soon is 1,
and unhurried or unspecified is 0). The corpus generator says these aren't
ground truth, so treat them as a rough check that packing doesn't make
answers worse, not as accuracy.
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def load(path, rows=None):
    records = {record["row"]: record for record in map(json.loads, Path(path).open(encoding="utf-8"))}
    return records if rows is None else {row: record for row, record in records.items() if row < rows}


def tvd(p, q):
    return 0.5 * sum(abs(p.get(key, 0.0) - q.get(key, 0.0)) for key in set(p) | set(q))


def difference(reference, candidate):
    """(agrees, distance) for one answer."""
    kind = reference["type"]
    if kind == "choice":
        return reference["choice"] == candidate["choice"], tvd(reference["probabilities"], candidate["probabilities"])
    if kind == "score":
        return round(reference["score"]) == round(candidate["score"]), abs(reference["score"] - candidate["score"])
    return (reference["noul"] >= 0.5) == (candidate["noul"] >= 0.5), abs(reference["noul"] - candidate["noul"])


def score_tvd(reference, candidate):
    return tvd(reference["probabilities"], candidate["probabilities"])


CONFIDENCE_BUCKETS = ((0.0, 0.8, "below 0.80"), (0.8, 0.95, "0.80-0.95"), (0.95, 1.01, "0.95 or higher"))


def totals(records):
    """Answers summed over every row: Choice option shares, mean Score, and the share of Nouls at 0.5 or above."""
    rows = list(records.values())
    out = {}
    for question, first in rows[0]["answers"].items():
        if first["type"] == "choice":
            counts = defaultdict(int)
            for row in rows:
                counts[row["answers"][question]["choice"]] += 1
            out[question] = {option: round(counts[option] / len(rows), 4) for option in sorted(first["probabilities"])}
        elif first["type"] == "score":
            out[question] = round(statistics.fmean(row["answers"][question]["score"] for row in rows), 4)
        else:
            out[question] = round(statistics.fmean(row["answers"][question]["noul"] >= 0.5 for row in rows), 4)
    return out


def compare(reference, candidate):
    rows = sorted(set(reference) & set(candidate))
    questions = list(reference[rows[0]]["answers"])
    out = {"rows": len(rows), "questions": {}}
    for question in questions:
        kind = reference[rows[0]]["answers"][question]["type"]
        agree, distance, level_tvd = [], [], []
        by_position = defaultdict(list)
        by_confidence = defaultdict(list)
        by_contrast = defaultdict(list)
        toward_majority = defaultdict(list)
        for row in rows:
            ref = reference[row]["answers"][question]
            cand = candidate[row]["answers"][question]
            same, gap = difference(ref, cand)
            agree.append(same)
            distance.append(gap)
            if kind == "score":
                level_tvd.append(score_tvd(ref, cand))
            if "position" in candidate[row]:
                by_position[candidate[row]["position"] // 8].append((same, gap))
            if kind == "choice":
                top = max(ref["probabilities"].values())
                label = next(name for low, high, name in CONFIDENCE_BUCKETS if low <= top < high)
                by_confidence[label].append((same, gap))
            if "contrast" in candidate[row]:
                group = "contrast" if candidate[row]["contrast"] else "majority"
                by_contrast[group].append((same, gap))
                if kind == "choice" and question == "department":
                    majority = candidate[row]["majority"]
                    shift = cand["probabilities"].get(majority, 0.0) - ref["probabilities"].get(majority, 0.0)
                    toward_majority[group].append(shift)
        result = {
            "type": kind,
            "agreement": round(statistics.fmean(agree), 4),
            "mean_distance": round(statistics.fmean(distance), 4),
            "max_distance": round(max(distance), 4),
            "large_differences": sum(gap >= 0.25 for gap in distance),
        }
        if level_tvd:
            result["mean_level_tvd"] = round(statistics.fmean(level_tvd), 4)
        if len(by_position) > 1:
            result["by_position"] = {
                f"{bucket * 8}-{bucket * 8 + 7}": {
                    "agreement": round(statistics.fmean(s for s, _ in values), 4),
                    "mean_distance": round(statistics.fmean(g for _, g in values), 4),
                    "rows": len(values),
                }
                for bucket, values in sorted(by_position.items())
            }
        if by_confidence:
            result["by_confidence"] = {
                name: {
                    "agreement": round(statistics.fmean(s for s, _ in by_confidence[name]), 4),
                    "mean_distance": round(statistics.fmean(g for _, g in by_confidence[name]), 4),
                    "rows": len(by_confidence[name]),
                }
                for _, _, name in CONFIDENCE_BUCKETS if by_confidence[name]
            }
        if by_contrast:
            result["by_contrast"] = {
                group: {
                    "agreement": round(statistics.fmean(s for s, _ in values), 4),
                    "mean_distance": round(statistics.fmean(g for _, g in values), 4),
                    "rows": len(values),
                    **({"mean_shift_toward_majority": round(statistics.fmean(toward_majority[group]), 4)} if toward_majority[group] else {}),
                }
                for group, values in sorted(by_contrast.items())
            }
        out["questions"][question] = result
    return out


URGENCY_LEVEL = {"today": 2, "soon": 1, "unhurried": 0, "unspecified": 0}


def authoring_labels():
    import corpus

    return [(group, URGENCY_LEVEL[timing]) for _, group, _, timing in corpus.generate()]


def label_match(records, labels):
    """Share of rows whose department matches the scenario group, and whose rounded urgency matches the deadline style."""
    department = [records[row]["answers"]["department"]["choice"] == labels[row][0] for row in records if labels[row][0] != "mixed"]
    urgency = [round(records[row]["answers"]["urgency"]["score"]) == labels[row][1] for row in records]
    return {"department": round(statistics.fmean(department), 4), "urgency": round(statistics.fmean(urgency), 4)}


def run_summary(path):
    summary = Path(path).with_suffix(".json")
    if not summary.exists():
        return None
    data = json.loads(summary.read_text())
    return {key: data[key] for key in (
        "strategy", "pointer", "content_key", "batch", "order", "concurrency", "rows", "calls", "wall_seconds",
        "rows_per_second", "input_tokens_per_row", "input_tokens_per_second", "cost_usd", "latency_ms", "outcomes", "splits",
    ) if key in data}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reference")
    parser.add_argument("candidates", nargs="+")
    parser.add_argument("--json", type=Path, help="also write everything to this file")
    parser.add_argument("--positions", action="store_true", help="print agreement by position in the call")
    parser.add_argument("--labels", action="store_true", help="also score department and urgency against the corpus's authoring metadata")
    parser.add_argument("--confidence", action="store_true", help="print Choice agreement by the reference's confidence")
    parser.add_argument("--totals", action="store_true", help="print each run's answers summed over all rows")
    parser.add_argument("--rows", type=int, help="compare only rows numbered below this")
    args = parser.parse_args()

    reference = load(args.reference, args.rows)
    report = {"reference": args.reference, "reference_run": run_summary(args.reference), "reference_totals": totals(reference), "candidates": {}}
    if args.totals:
        print(f"{Path(args.reference).stem} totals: {report['reference_totals']}")
    labels = authoring_labels() if args.labels else None
    for path in args.candidates:
        candidate = load(path, args.rows)
        result = compare(reference, candidate)
        result["run"] = run_summary(path)
        result["totals"] = totals(candidate)
        if labels:
            rows = set(reference) & set(candidate)
            result["authoring_match"] = {
                "reference": label_match({row: reference[row] for row in rows}, labels),
                "candidate": label_match({row: candidate[row] for row in rows}, labels),
            }
        report["candidates"][path] = result
        run = result["run"] or {}
        print(f"\n{Path(path).stem}: {result['rows']} rows", end="")
        if run:
            print(f", {run['rows_per_second']} rows/s, {run['input_tokens_per_row']} tokens/row, p50 {run['latency_ms']['p50']} ms, {run['outcomes']}", end="")
        print()
        if args.totals:
            print(f"  totals: {result['totals']}")
        if "authoring_match" in result:
            ref_match, cand_match = result["authoring_match"]["reference"], result["authoring_match"]["candidate"]
            print("  matches authoring metadata (reference -> candidate): " + ", ".join(
                f"{key} {ref_match[key]:.2%} -> {cand_match[key]:.2%}" for key in ref_match))
        for question, q in result["questions"].items():
            extra = f"  level TVD {q['mean_level_tvd']:.4f}" if "mean_level_tvd" in q else ""
            print(f"  {question:14} {q['type']:6}  agree {q['agreement']:.2%}  mean diff {q['mean_distance']:.4f}  max {q['max_distance']:.2f}  ≥0.25: {q['large_differences']}{extra}")
            if args.confidence and "by_confidence" in q:
                cells = "  ".join(f"{name}: {v['agreement']:.1%} ({v['rows']} rows)" for name, v in q["by_confidence"].items())
                print(f"      by reference confidence  {cells}")
            if args.positions and "by_position" in q:
                cells = "  ".join(f"{bucket}: {v['agreement']:.1%}/{v['mean_distance']:.3f}" for bucket, v in q["by_position"].items())
                print(f"      by position  {cells}")
            if "by_contrast" in q:
                for group, v in q["by_contrast"].items():
                    shift = f"  shift toward majority {v['mean_shift_toward_majority']:+.4f}" if "mean_shift_toward_majority" in v else ""
                    print(f"      {group:8} ({v['rows']} rows)  agree {v['agreement']:.2%}  mean diff {v['mean_distance']:.4f}{shift}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
