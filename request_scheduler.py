"""Bounded parallel SDK calls, with one adaptive controller for all attempts.

Limits checked at https://docs.typesafe.ai/models on 2026-09-28. The controller
belongs to the consuming thread; worker threads only perform single HTTP calls.
"""

import json
import logging
import math
import random
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

from typesafe_sdk import RetryPolicy, TypeSafeAPIConnectionError, TypeSafeAPIError


DEFAULT_REQUESTS_PER_MINUTE = 1_200
DEFAULT_TOKENS_PER_SECOND = 250_000
DEFAULT_CONCURRENCY = 16
DEFAULT_MAX_RETRIES = 8
LOGGER = logging.getLogger(__name__)
NO_SDK_RETRIES = RetryPolicy(max_retries=0)


def positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def validate_count(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def retry_after_seconds(headers, *, wall_time=None):
    """Support milliseconds, numeric seconds, and HTTP dates, on any HTTP error."""
    headers = {name.lower(): value for name, value in headers.items()}
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            delay = float(raw) * scale
        except (TypeError, ValueError, OverflowError):
            if name != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = max(0.0, date.timestamp() - (time.time() if wall_time is None else wall_time))
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return None


@dataclass
class Attempt:
    generation: int
    rate: float
    started_at: float
    reserved_tokens: int
    payload_bytes: int


class AdaptiveRateLimiter:
    """Pace starts; learn a sustainable rate from congestion and clean windows.

    Start 5% below the configured limits. On congestion, reduce the request rate
    once per generation of in-flight requests. Recover 10% per clean minute up to
    95% of the last rejected rate; cautiously probe that ceiling every five clean
    minutes. A 529 is a capacity signal, not proof of an account's fixed quota.

    Token reservations use payload bytes plus prompt overhead, raised if observed
    usage exceeds that estimate. These are deliberately conservative estimates,
    not an official tokenizer. Retries consume both request and token budgets.
    """

    def __init__(
        self, requests_per_minute=DEFAULT_REQUESTS_PER_MINUTE,
        tokens_per_second=DEFAULT_TOKENS_PER_SECOND, *, headroom=0.95,
        recovery_seconds=60.0, probe_seconds=300.0,
        backoff_initial=1.0, backoff_max=60.0,
    ):
        self.requests_per_minute = positive_number(requests_per_minute, "requests_per_minute")
        self.tokens_per_second = positive_number(tokens_per_second, "tokens_per_second")
        positive_number(headroom, "headroom")
        if headroom >= 1:
            raise ValueError("headroom must be less than 1")
        self.headroom = headroom
        self.recovery_seconds = positive_number(recovery_seconds, "recovery_seconds")
        self.probe_seconds = positive_number(probe_seconds, "probe_seconds")
        self.backoff_initial = positive_number(backoff_initial, "backoff_initial")
        self.backoff_max = positive_number(backoff_max, "backoff_max")
        self.learned_limit_rps = requests_per_minute / 60
        self.target_rps = self.learned_limit_rps * headroom
        self.token_budget = tokens_per_second * headroom
        self.generation = 0
        self.attempts = 0
        self.successes = 0
        self.input_tokens = 0
        self.failures = Counter()
        self.peak_in_flight = 0
        self._next_start = 0.0
        self._cooldown_until = 0.0
        self._healthy_since = None
        self._clean_successes = 0
        self._can_reduce = True
        self._last_probe = 0.0
        self._token_window = deque()
        self._tokens_per_byte = 1.0

    def estimate_tokens(self, payload_bytes):
        return math.ceil(payload_bytes * self._tokens_per_byte)

    def delay(self, now, tokens):
        if tokens > self.token_budget:
            raise ValueError(
                "One request's conservative token reservation exceeds the per-second "
                "budget; increase --tokens-per-second or reduce the state/questions"
            )
        while self._token_window and self._token_window[0].started_at + 1 <= now:
            self._token_window.popleft()
        total = sum(attempt.reserved_tokens for attempt in self._token_window) + tokens
        ready = max(self._next_start, self._cooldown_until)
        for attempt in self._token_window:
            if total <= self.token_budget:
                break
            ready = max(ready, attempt.started_at + 1)
            total -= attempt.reserved_tokens
        return max(0.0, ready - now)

    def started(self, now, payload_bytes):
        tokens = self.estimate_tokens(payload_bytes)
        attempt = Attempt(self.generation, self.target_rps, now, tokens, payload_bytes)
        self._token_window.append(attempt)
        self._next_start = now + 1 / self.target_rps
        self.attempts += 1
        if self._healthy_since is None:
            self._healthy_since = now
            self._last_probe = now
        return attempt

    def succeeded(self, now, attempt, input_tokens):
        self.successes += 1
        if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0:
            self.input_tokens += input_tokens
            attempt.reserved_tokens = max(attempt.reserved_tokens, input_tokens)
            self._tokens_per_byte = max(self._tokens_per_byte, 1.1 * input_tokens / attempt.payload_bytes)
        if attempt.generation != self.generation or now < self._cooldown_until:
            return
        self._can_reduce = True
        self._clean_successes += 1
        if self._clean_successes < 10 or now - self._healthy_since < self.recovery_seconds:
            return
        if now - self._last_probe >= self.probe_seconds:
            self.learned_limit_rps = min(self.requests_per_minute / 60, self.learned_limit_rps * 1.05)
            self._last_probe = now
        rate = min(self.target_rps * 1.1, self.learned_limit_rps * self.headroom)
        if rate > self.target_rps:
            self.target_rps = rate
            self.generation += 1
            LOGGER.info("Healthy window: target now %.3f requests/s", rate)
        self._healthy_since = now
        self._clean_successes = 0

    def failed(self, now, attempt, error, retry_number):
        status = error.status if isinstance(error, TypeSafeAPIError) else "connection"
        self.failures[status] += 1
        backoff = min(self.backoff_max, self.backoff_initial * 2 ** min(retry_number, 20) * (1 + random.random() * 0.25))
        header_delay = retry_after_seconds(error.headers) if isinstance(error, TypeSafeAPIError) else None
        delay = max(backoff, header_delay or 0.0)
        self._cooldown_until = max(self._cooldown_until, now + delay)
        self._healthy_since = self._cooldown_until
        self._clean_successes = 0
        # Several responses can describe the same overload. Do not multiply the
        # decrease by the number of workers that were already in flight.
        if status in (429, 529) and attempt.generation == self.generation and self._can_reduce:
            self.learned_limit_rps = min(self.learned_limit_rps, attempt.rate)
            reduction = 0.8 if status == 429 else 0.5
            self.target_rps = min(self.target_rps * reduction, self.learned_limit_rps * self.headroom)
            self.generation += 1
            # Further rejections without an intervening success may simply mean
            # the same minute's quota has not reset. Back off longer without
            # mistaking every such rejection for an even smaller rate limit.
            self._can_reduce = False
            self._last_probe = self._cooldown_until
            self._next_start = max(self._next_start, now + 1 / self.target_rps)
        LOGGER.warning("%s: pausing all new requests for %.3fs; target %.3f requests/s", status, delay, self.target_rps)


def retryable(error):
    return isinstance(error, TypeSafeAPIConnectionError) or (
        isinstance(error, TypeSafeAPIError)
        and (error.status in (408, 429) or 500 <= error.status < 600)
    )


def evaluate_chunk(states, client, questions, model, limiter, *, concurrency, max_retries):
    """Finish one bounded chunk, preserving order and leaving no work at yield.

    Only the coordinator submits/retries calls. It observes completed failures
    before dispatching more work, and never queues more futures than concurrency.
    SDK retries are disabled so every HTTP attempt passes through this controller.
    """
    question_json = {name: question.model_dump(mode="json") for name, question in questions.items()}
    sizes = [
        len(json.dumps({"state": state, "questions": question_json, "model": model}, ensure_ascii=False).encode("utf-8"))
        + 256 * len(questions)  # Prompt formatting overhead; not a token count.
        for state in states
    ]
    pending = deque(range(len(states)))
    retries = [0] * len(states)
    responses = [None] * len(states)
    active = {}
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="jev")
    try:
        while pending or active:
            # Handle successes and failures in dispatch order when several finish
            # together, making generation changes independent of set iteration.
            for future in [future for future in active if future.done()]:
                index, attempt = active.pop(future)
                try:
                    response = future.result()
                except Exception as error:
                    if not retryable(error):
                        raise
                    limiter.failed(time.monotonic(), attempt, error, retries[index])
                    if retries[index] >= max_retries:
                        raise
                    retries[index] += 1
                    pending.appendleft(index)
                else:
                    responses[index] = response
                    limiter.succeeded(time.monotonic(), attempt, response.usage.input_tokens)

            delay = None
            if pending and len(active) < concurrency:
                index = pending[0]
                now = time.monotonic()
                delay = limiter.delay(now, limiter.estimate_tokens(sizes[index]))
                if delay <= 0:
                    pending.popleft()
                    attempt = limiter.started(now, sizes[index])
                    future = pool.submit(
                        client.system_one, state=states[index], questions=questions,
                        model=model, retry=NO_SDK_RETRIES,
                    )
                    active[future] = index, attempt
                    limiter.peak_in_flight = max(limiter.peak_in_flight, len(active))
                    continue
            if active:
                wait(active, timeout=delay, return_when=FIRST_COMPLETED)
            elif pending:
                time.sleep(delay)
    finally:
        # On failure/KeyboardInterrupt, no retries or new states are submitted.
        # Already-running HTTP calls finish under the client's network timeout.
        pool.shutdown(wait=True, cancel_futures=True)
    return responses
