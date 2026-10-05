"""Pack several states into each TypeSafe call by moving each state into its questions.

With rows_per_call above 1, an upstream call has an empty state. For each row
in the call, it repeats every question with the row's state added to its
instructions:

    "Is a refund requested?"         -> {"state": <row's state>, "question": "Is a refund requested?"}
    {"question": "...", "note": "..."} -> {"state": <row's state>, "question": "...", "note": "..."}

TypeSafe evaluates the questions in a call independently, so rows can't affect
each other's answers. A backticked path in instructions or criteria that
resolves against the row's state, such as `ticket.body`, becomes
`state.ticket.body`. Question keys are never sent to the model, so each packed
question gets a key that maps back to its row and question.
"""

import asyncio
import json
import re
from collections import deque
from dataclasses import dataclass, field

from typesafe_sdk import Score, TypeSafeAPIError

STATE_FIELD = "state"
MAX_ROWS_PER_CALL = 256
# TypeSafe's documented limit is 64k input tokens per request. Sizes are
# estimated from JSON length; a call TypeSafe still rejects is split in two.
MAX_CALL_TOKENS = 48_000
CALL_OVERHEAD_TOKENS = 260
CHARS_PER_TOKEN = 3.5
KEY_CHARS = 12
SPLIT_STATUSES = {400, 413, 422}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_INDEX = re.compile(r"\[(\d+)\]")
_QUOTED = re.compile(r"""\[(?:"((?:[^"\\]|\\.)*)"|'([^'\\]*)')\]""")
_TICKED = re.compile(r"`([^`\n]+)`")


class PackingError(ValueError):
    """Questions or states that can't be packed as they are."""


def parse_path(text):
    """Split a backticked dot-and-index path such as ticket.messages[0].text, or return None."""
    parts, pos = [], 0
    while pos < len(text):
        if pos and text[pos] == ".":
            match = _IDENT.match(text, pos + 1)
            part = match and match.group()
        elif not pos and (match := _IDENT.match(text)):
            part = match.group()
        elif match := _INDEX.match(text, pos):
            part = int(match.group(1))
        elif match := _QUOTED.match(text, pos):
            double, single = match.groups()
            part = json.loads(f'"{double}"') if double is not None else single
        else:
            return None
        if not match:
            return None
        parts.append(part)
        pos = match.end()
    return parts or None


def format_path(parts):
    out = []
    for i, part in enumerate(parts):
        if isinstance(part, int):
            out.append(f"[{part}]")
        elif _IDENT.fullmatch(part):
            out.append(f".{part}" if i else part)
        else:
            out.append(f"[{json.dumps(part)}]")
    return "".join(out)


def resolves(value, parts):
    for part in parts:
        if isinstance(part, int) and isinstance(value, list) and part < len(value):
            value = value[part]
        elif isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return False
    return True


def strings(value):
    """Every string inside a JSON value, except object keys."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def replace_paths(value, replacements):
    if isinstance(value, str):
        return _TICKED.sub(lambda match: replacements.get(match.group(), match.group()), value)
    if isinstance(value, list):
        return [replace_paths(item, replacements) for item in value]
    if isinstance(value, dict):
        # Keys stay as they are: Choice options are keys, and so are instructions fields.
        return {key: replace_paths(item, replacements) for key, item in value.items()}
    return value


@dataclass
class Template:
    """One question, prepared once per request and lifted once per row."""

    name: str
    question: dict
    # Backticked paths that may name part of a state: (backticked text, parsed path).
    paths: list
    # Backticked paths that start with a field of the instructions object.
    local_paths: list
    legend: dict | None
    size: int


def templates_for(questions):
    """Prepare each question for packing, or raise PackingError if one can't be packed."""
    templates = []
    for name, question in questions.items():
        wire = question.model_dump(mode="json")
        instructions = wire.get("instructions")
        local = set(instructions) if isinstance(instructions, dict) else set()
        if STATE_FIELD in local:
            raise PackingError(
                f"Question {name!r} has a {STATE_FIELD!r} field in its instructions. With rows_per_call "
                f"above 1, Jevaro puts each row's state there; rename that field or omit rows_per_call."
            )
        paths, local_paths, seen = [], [], set()
        for text in (*strings(instructions), *strings(wire.get("criteria"))):
            for match in _TICKED.finditer(text):
                parts = parse_path(match.group(1))
                if parts is None or match.group() in seen:
                    continue
                seen.add(match.group())
                if isinstance(parts[0], str) and parts[0] in local:
                    local_paths.append((match.group(), parts))
                else:
                    paths.append((match.group(), parts))
        legend = dict(enumerate(wire["criteria"])) if isinstance(question, Score) else None
        # The lifted question's JSON without its state, plus room for its packed key.
        size = len(json.dumps(lift(wire, "", {}), ensure_ascii=False)) - 2 + KEY_CHARS
        templates.append(Template(name, wire, paths, local_paths, legend, size))
    return templates


def lift(question, state, replacements):
    """A copy of question whose instructions carry state, with its paths rewritten."""
    lifted = dict(question)
    instructions = question.get("instructions")
    if replacements:
        instructions = replace_paths(instructions, replacements)
        if "criteria" in question:
            lifted["criteria"] = replace_paths(question["criteria"], replacements)
    if isinstance(instructions, dict):
        lifted["instructions"] = {STATE_FIELD: state, **instructions}
    elif instructions is None:
        lifted["instructions"] = {STATE_FIELD: state}
    else:
        lifted["instructions"] = {STATE_FIELD: state, "question": instructions}
    return lifted


def state_checker(templates):
    """A function that rejects a conflicting state, or None when no state can conflict.

    A question can refer to a field of its own instructions object by name. If a
    state has a top-level field with that name too, the reference is ambiguous
    once the state moves into the instructions.
    """
    if not any(template.local_paths for template in templates):
        return None
    return lambda state, row: check_state(templates, state, row)


def check_state(templates, state, row):
    if not isinstance(state, dict):
        return
    for template in templates:
        for text, parts in template.local_paths:
            if parts[0] in state:
                raise PackingError(
                    f"Row {row}: question {template.name!r} refers to {text}, and {parts[0]!r} names a field "
                    f"of both its instructions and this row's state. With rows_per_call above 1, the state "
                    f"moves into the instructions, so {text} would mean only the instructions field. Rename "
                    f"that instructions field or omit rows_per_call."
                )


@dataclass
class Call:
    """One upstream call: an empty state and the lifted questions of one or more rows."""

    first_row: int
    questions: dict = field(default_factory=dict)
    # Packed question key -> (row, question name, Score legend to restore or None).
    units: dict = field(default_factory=dict)
    rows: int = 0
    last_row: int = -1
    ends_row: bool = True
    tokens: float = CALL_OVERHEAD_TOKENS


def plan(states, templates, rows_per_call, max_tokens=None):
    """Yield calls in row order, each with up to rows_per_call rows and about max_tokens tokens.

    A row whose questions don't all fit in one call continues in the next one.
    """
    max_tokens = MAX_CALL_TOKENS if max_tokens is None else max_tokens
    call = Call(0)
    for row, state in enumerate(states):
        if call.rows == rows_per_call:
            yield call
            call = Call(row)
        state_size = len(json.dumps(state, ensure_ascii=False))
        for number, template in enumerate(templates):
            replacements = {}
            for text, parts in template.paths:
                if resolves(state, parts):
                    replacements[text] = f"`{format_path([STATE_FIELD, *parts])}`"
            tokens = (template.size + state_size + 8 * len(replacements)) / CHARS_PER_TOKEN
            if call.units and call.tokens + tokens > max_tokens:
                call.ends_row = number == 0
                yield call
                call = Call(row)
            if call.last_row != row:
                call.rows += 1
                call.last_row = row
            key = f"{row - call.first_row}.{number}"
            call.questions[key] = lift(template.question, state, replacements)
            legend = template.legend if replacements and template.legend is not None else None
            call.units[key] = (row, template.name, legend)
            call.tokens += tokens
    if call.units:
        call.ends_row = True
        yield call


async def send(client, call, model):
    """Answers for every unit in call, as {(row, name): answer}, splitting the call if TypeSafe rejects it."""
    try:
        response = await client.system_one(state="", questions=call.questions, model=model)
    except TypeSafeAPIError as error:
        if error.status not in SPLIT_STATUSES or len(call.units) < 2:
            raise
        keys = list(call.units)
        halves = []
        for part in (keys[:len(keys) // 2], keys[len(keys) // 2:]):
            half = Call(call.first_row)
            half.questions = {key: call.questions[key] for key in part}
            half.units = {key: call.units[key] for key in part}
            halves.append(half)
        first, second = await asyncio.gather(*(send(client, half, model) for half in halves))
        return first | second
    answers = {}
    for key, (row, name, legend) in call.units.items():
        answer = response.answers.get(key)
        if answer is None:
            raise ValueError(f"TypeSafe returned no answer for row {row}, question {name!r}")
        if legend is not None:
            # The API echoes the criteria it was sent; report the ones the caller sent.
            if len(answer.legend) != len(legend):
                raise ValueError(f"TypeSafe returned an unexpected legend for row {row}, question {name!r}")
            answer = answer.model_copy(update={"legend": legend})
        answers[row, name] = answer
    return answers


@dataclass
class Row:
    """One row's answers, keyed by question name, like a TypeSafe response."""

    answers: dict


def succeeded(task):
    return task.done() and not task.cancelled() and task.exception() is None


async def packed_responses(client, states, templates, model, concurrency, rows_per_call):
    """Yield runs of Rows in input order, keeping up to concurrency calls pending or awaiting their turn."""
    names = [template.name for template in templates]
    calls = plan(states, templates, rows_per_call)
    pending = deque()
    exhausted = False
    answers = {}
    next_row = 0

    def submit():
        nonlocal exhausted
        if exhausted:
            return
        call = next(calls, None)
        if call is None:
            exhausted = True
            return
        task = asyncio.create_task(send(client, call, model))
        pending.append((call, task))

    try:
        for _ in range(concurrency):
            submit()
        while pending:
            call, task = pending[0]
            answers.update(await task)
            pending.popleft()
            ready = call.last_row if call.ends_row else call.last_row - 1
            finished = 1
            # A failed call ends the run, so the rows before it are sent before the stream aborts.
            while pending and succeeded(pending[0][1]):
                call, task = pending.popleft()
                answers.update(task.result())
                ready = call.last_row if call.ends_row else call.last_row - 1
                finished += 1
            for _ in range(finished):
                submit()
            if ready >= next_row:
                run = [Row({name: answers.pop((row, name)) for name in names}) for row in range(next_row, ready + 1)]
                next_row = ready + 1
                yield run
    finally:
        for _, task in pending:
            task.cancel()
        await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
