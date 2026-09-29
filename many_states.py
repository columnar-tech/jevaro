"""Run a small sample by default; expose the results as a RecordBatchReader."""

import argparse
import json
import logging
import math
import sys
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path

import pyarrow as pa
from typesafe_sdk import RetryPolicy, TypeSafeClient

from all_types import QUESTIONS
from arrow_results import evaluate_states
from request_scheduler import (
    AdaptiveRateLimiter, DEFAULT_CONCURRENCY, DEFAULT_MAX_RETRIES,
    DEFAULT_REQUESTS_PER_MINUTE, DEFAULT_TOKENS_PER_SECOND, LOGGER,
)

# Official input pricing checked on 2026-09-28. Match the resolved model so an
# alias moving to a differently priced model cannot silently reuse this rate.
INPUT_USD_PER_MILLION = {"jev-1.13.0": Decimal("0.042")}
PRICING_SOURCE = "https://docs.typesafe.ai/models"


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def nonnegative_decimal(value):
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("must be a nonnegative finite number") from error
    if not number.is_finite() or number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative finite number")
    return number


def estimate_cost(states, client, *, model="jev-latest", input_price_per_million=None):
    """Estimate input cost from one actual request, including the shared questions.

    No official local Jev tokenizer is exposed by the SDK/docs. Sample the state
    nearest the mean character length, then assume its token usage is typical.
    Character counts select the sample; they are never substituted for token counts.
    """
    if isinstance(states, (str, bytes)):
        raise TypeError("states must be an iterable of messages, not a single string")
    states = list(states)
    if not states:
        raise ValueError("No states selected for cost estimation")
    if any(not isinstance(state, str) or not state.strip() for state in states):
        raise ValueError("Every state must be a nonempty string")
    if input_price_per_million is not None:
        input_price_per_million = nonnegative_decimal(str(input_price_per_million))
    count = len(states)
    total_characters = sum(map(len, states))
    index = min(range(count), key=lambda i: abs(len(states[i]) * count - total_characters))
    response = client.system_one(
        state=states[index],
        questions={name: question.model_copy(deep=True) for name, question in QUESTIONS.items()},
        model=model,
        retry=RetryPolicy(max_retries=0),
    )
    tokens = response.usage.input_tokens
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise ValueError("API did not report a valid usage.input_tokens; cannot estimate cost")
    rate = input_price_per_million
    if rate is None:
        rate = INPUT_USD_PER_MILLION.get(response.model)
        if rate is None:
            raise ValueError(
                f"No recorded input price for resolved model {response.model!r}; "
                "supply --input-price-per-million"
            )
    estimated_tokens = tokens * count
    sample_cost = Decimal(tokens) * rate / Decimal(1_000_000)
    return {
        "method": "single_request_sample",
        "states": count,
        "requested_model": model,
        "resolved_model": response.model,
        "sample_line_number": index + 1,
        "sample_state_characters": len(states[index]),
        "mean_state_characters": round(total_characters / count, 2),
        "sample_input_tokens": tokens,
        "estimated_total_input_tokens": estimated_tokens,
        "input_usd_per_million_tokens": format(rate, "f"),
        "estimated_input_cost_usd": format(sample_cost * count, "f"),
        "sample_request_cost_usd": format(sample_cost, "f"),
        "pricing_source": PRICING_SOURCE if input_price_per_million is None else "user override",
        "pricing_checked_on": "2026-09-28" if input_price_per_million is None else None,
        "api_calls": 1,
        "note": (
            "Approximate: one state's input usage, including the questions, is multiplied "
            "by the selected state count. Token lengths vary. The probe cost is shown "
            "separately; a subsequent full run would incur the estimated run cost as well."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", type=Path, default=Path(__file__).with_name("states.txt"))
    parser.add_argument("--limit", type=positive_int, help="maximum states (default: 3 for evaluation, all for cost estimation)")
    parser.add_argument("--batch-size", type=positive_int, default=128, help="rows per Arrow batch, not API requests per call")
    parser.add_argument("--concurrency", type=positive_int, default=DEFAULT_CONCURRENCY, help="maximum in-flight requests (default: 16)")
    parser.add_argument("--requests-per-minute", type=positive_float, default=DEFAULT_REQUESTS_PER_MINUTE, help="request ceiling before 5%% headroom (default: 1200)")
    parser.add_argument("--tokens-per-second", type=positive_float, default=DEFAULT_TOKENS_PER_SECOND, help="token ceiling before 5%% headroom (default: 250000)")
    parser.add_argument("--max-retries", type=nonnegative_int, default=DEFAULT_MAX_RETRIES, help="retries per state after its first attempt (default: 8)")
    parser.add_argument("--quiet", action="store_true", help="omit schema and per-row JSON; still report progress and retries")
    parser.add_argument("--model", default="jev-latest")
    parser.add_argument("--output", type=Path, help="optionally write an Arrow IPC stream")
    parser.add_argument("--estimate-cost", action="store_true", help="estimate input cost with one sample API call, then exit")
    parser.add_argument("--input-price-per-million", type=nonnegative_decimal, help="override USD per million input tokens for --estimate-cost")
    args = parser.parse_args()
    if args.estimate_cost and args.output:
        parser.error("--output cannot be combined with --estimate-cost")
    if args.input_price_per_million is not None and not args.estimate_cost:
        parser.error("--input-price-per-million requires --estimate-cost")
    if args.output and args.output.resolve() == args.states.resolve():
        parser.error("--output must not overwrite the states file")
    limit = args.limit if args.limit is not None else (None if args.estimate_cost else 3)

    with ExitStack() as stack:
        source = stack.enter_context(args.states.open(encoding="utf-8"))
        states = (line.rstrip("\r\n") for line in islice(source, limit))
        client = stack.enter_context(TypeSafeClient())
        if args.estimate_cost:
            try:
                estimate = estimate_cost(
                    states, client, model=args.model,
                    input_price_per_million=args.input_price_per_million,
                )
            except ValueError as error:
                parser.error(str(error))
            print(json.dumps(estimate, indent=2))
            return
        logging.basicConfig(format="%(message)s", level=logging.WARNING)
        LOGGER.setLevel(logging.INFO)
        limiter = AdaptiveRateLimiter(args.requests_per_minute, args.tokens_per_second)
        print(
            f"Up to {args.concurrency} concurrent requests; starting at {limiter.target_rps:g} requests/s "
            f"and {limiter.token_budget:g} estimated tokens/s.", file=sys.stderr,
        )
        reader = stack.enter_context(evaluate_states(
            states, client, batch_size=args.batch_size, model=args.model,
            concurrency=args.concurrency, max_retries=args.max_retries, limiter=limiter,
        ))
        if not args.quiet:
            print(reader.schema)
        writer = None
        if args.output:
            sink = stack.enter_context(pa.OSFile(str(args.output), "wb"))
            writer = stack.enter_context(pa.ipc.new_stream(sink, reader.schema))
        count = 0
        for batch in reader:
            if writer:
                writer.write_batch(batch)
            if not args.quiet:
                for row in batch.to_pylist():
                    print(json.dumps(row, ensure_ascii=False))
            count += batch.num_rows
            print(
                f"Read {count} states; {limiter.attempts} HTTP attempts; "
                f"target {limiter.target_rps:.3f} requests/s.", file=sys.stderr, flush=True,
            )
        print(
            f"Read {count} states using {limiter.attempts} HTTP attempts "
            f"(429: {limiter.failures[429]}, 529: {limiter.failures[529]}; "
            f"peak in flight: {limiter.peak_in_flight}; reported input tokens: {limiter.input_tokens})."
        )


if __name__ == "__main__":
    main()
