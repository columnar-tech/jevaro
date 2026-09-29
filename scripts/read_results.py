"""Print the saved Arrow results as JSON lines. Requires only PyArrow.

No SDK, API key, original questions, or registered extension classes are needed.
PyArrow exposes unregistered extensions as their physical storage plus metadata.
"""

import argparse
import json
from contextlib import closing
from itertools import islice

import pyarrow as pa


def decode_answer(value, kind, metadata):
    if value is None:
        return None
    if kind == "choice":
        labels = metadata["labels"]
        return {
            "type": kind,
            **value,
            "choice": labels[value["choice"]],
            "probabilities": dict(zip(labels, value["probabilities"], strict=True)),
        }
    if kind == "score":
        legend = dict(enumerate(metadata["legend"]))
        return {
            "type": kind,
            **value,
            "legend": legend,
            "probabilities": dict(zip(legend, value["probabilities"], strict=True)),
        }
    if kind == "noul":
        return {"type": kind, "noul": value}
    raise ValueError(f"Unsupported answer type: {kind!r}")


def read_rows(path):
    with pa.ipc.open_stream(path) as reader:
        metadata = {}
        for field in reader.schema:
            attributes = field.metadata or {}
            extension = attributes.get(b"ARROW:extension:name")
            if extension is None:
                continue
            info = json.loads(attributes[b"ARROW:extension:metadata"])
            kind = extension.decode().rsplit(".", 1)[-1]
            if (kind not in ("choice", "score", "noul")
                    or extension not in (f"jev_demo.{kind}".encode(), f"jevaro.{kind}".encode())):
                raise ValueError(f"Unsupported extension metadata for {field.name!r}")
            metadata[field.name] = kind, info

        for batch in reader:
            for row in batch.to_pylist():
                for name, (kind, info) in metadata.items():
                    row[name] = decode_answer(row[name], kind, info)
                yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="parallel_results.arrows")
    parser.add_argument("--limit", type=int, help="print only the first N rows (default: all)")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be nonnegative")

    with closing(read_rows(args.path)) as rows:
        for row in islice(rows, args.limit):
            print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
