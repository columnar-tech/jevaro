"""Evaluate three customer messages through Jevaro; optionally save Arrow IPC."""

import argparse
import json
from contextlib import ExitStack

import pyarrow as pa
from jevaro import Choice, Noul, Score, TypeSafeClient

QUESTIONS = {
    "department": Choice(
        instructions="Which team should handle this customer message?",
        criteria={"returns": "Returns, exchanges, refunds", "shipping": "Delivery problems",
                  "billing": "Charges and payments", "other": "None of these teams fits"},
    ),
    "urgency": Score(
        instructions="How soon does the customer want a resolution?",
        criteria=["No deadline or can wait", "Within the next few days", "Today or immediately"],
    ),
    "refund": Noul(instructions="Is the customer requesting a refund?"),
}
STATES = [
    "The shoes don't fit. Please refund my money today.",
    "My parcel is marked delivered but isn't here. Please help me find it.",
    "I was charged twice for my coffee maker. Please fix the duplicate charge this week.",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="also write an Arrow IPC stream to this file")
    args = parser.parse_args()
    with ExitStack() as stack:
        client = stack.enter_context(TypeSafeClient())
        reader = stack.enter_context(client.system_one(states=STATES, questions=QUESTIONS))
        metadata = reader.schema.field("department").metadata
        labels = json.loads(metadata[b"ARROW:extension:metadata"])["labels"]
        writer = stack.enter_context(pa.ipc.new_stream(args.output, reader.schema)) if args.output else None
        for batch in reader:
            if writer:
                writer.write_batch(batch)
            for row in batch.to_pylist():
                print(json.dumps({"department": labels[row["department"]["choice"]],
                                  "urgency": row["urgency"]["score"], "refund": row["refund"]}))


if __name__ == "__main__":
    main()
