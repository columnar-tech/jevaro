"""Pack many independent TypeSafe requests into fewer API calls, then unpack the answers.

Each input is an ordinary TypeSafe request: a state, a map of questions, and a
model. A packed call evaluates several of them at once, and it's still an
ordinary request to the same endpoint. Question keys are never sent to the
model (see the API reference), so each packed question gets a new key that maps
back to its row and its original key.

Strategies:

- "single": no packing; one call per request, sent exactly as given.
- "index": the state becomes {"items": [s0, s1, ...]} and each question points
  at its row as `items[i]`.
- "keyed": the state becomes {"item_0": s0, "item_1": ...} and each question
  points at `item_i`. With key_names="animals", each row is named after a
  different animal instead, such as {"walrus": s0, "heron": s1, ...}.
- "lift": the state becomes "" and each question carries its own row's state
  in its instructions, so no question can see another row.

A question can name part of its state with a backticked dot-and-index path,
such as `ticket.messages[0].text`. When a path resolves against the row's
state, it's rewritten to the row's place in the packed request, such as
`items[3].ticket.messages[0].text`. Backticked text that doesn't resolve, or
that names a field of the question's own instructions object, is left alone.
"""

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

STRATEGIES = ("single", "index", "keyed", "lift")
POINTERS = ("about", "prefix", "scope")
KEY_NAMES = ("numbers", "animals")

# Distinct, everyday animals with one-word names, for keyed rows. Animals whose
# names suggest speed, delay, or temper (cheetah, sloth, hornet) are left out.
ANIMALS = (
    "aardvark", "albatross", "alpaca", "antelope", "armadillo", "badger", "beaver", "bison",
    "buffalo", "camel", "caribou", "chameleon", "chinchilla", "cormorant", "coyote", "crane",
    "dolphin", "donkey", "dugong", "eagle", "elephant", "emu", "ferret", "flamingo",
    "gazelle", "gecko", "gerbil", "giraffe", "gorilla", "hamster", "hedgehog", "heron",
    "hippo", "ibis", "iguana", "jackal", "kangaroo", "koala", "lemur", "llama",
    "lobster", "lynx", "manatee", "meerkat", "mongoose", "moose", "narwhal", "ocelot",
    "octopus", "orca", "ostrich", "otter", "panda", "pelican", "penguin", "platypus",
    "porcupine", "quokka", "raccoon", "reindeer", "salamander", "seahorse", "toucan", "walrus",
    "yak", "zebra",
)

# Documented limits for jev-1.13: 64k tokens per request, and 32k for the state
# plus the longest question. Token counts are estimated from JSON length, so
# leave a margin.
MAX_REQUEST_TOKENS = 48_000
MAX_STATE_PLUS_QUESTION_TOKENS = 24_000
CHARS_PER_TOKEN = 3.5

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_INDEX = re.compile(r"\[(\d+)\]")
_QUOTED = re.compile(r"""\[(?:"((?:[^"\\]|\\.)*)"|'([^'\\]*)')\]""")
_TICKED = re.compile(r"`([^`\n]+)`")


@dataclass
class Request:
    """One ordinary TypeSafe request."""

    state: Any
    questions: dict[str, dict]
    model: str = "jev-latest"


@dataclass
class Packed:
    """One API call standing in for one or more requests."""

    body: dict
    rows: list[int]
    # Packed question key -> (position in rows, original question key).
    keys: dict[str, tuple[int, str]] = field(default_factory=dict)
    # Packed question key -> {rewritten text: original text}, to undo rewrites
    # that the API echoes back in a Score legend.
    restore: dict[str, dict[str, str]] = field(default_factory=dict)
    # keyed: each row's key in the packed state, in row order.
    names: list[str] = field(default_factory=list)


def parse_path(text):
    """Split a dot-and-index path into keys and indexes, or return None if it isn't one."""
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
    """The inverse of parse_path."""
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
    """Whether a parsed path names something inside value."""
    for part in parts:
        if isinstance(part, int) and isinstance(value, list) and part < len(value):
            value = value[part]
        elif isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return False
    return True


def rewrite(value, state, prefix, local=(), changes=None):
    """Prefix every backticked path in value that resolves against state.

    Dict keys are never changed, because Choice options are dict keys. Paths
    whose first key is in local name the question's own instructions fields.
    Each rewrite is recorded in changes as {new: old}.
    """
    changes = {} if changes is None else changes

    def replace(match):
        parts = parse_path(match.group(1))
        if parts is None or (isinstance(parts[0], str) and parts[0] in local) or not resolves(state, parts):
            return match.group()
        new = f"`{format_path(prefix + parts)}`"
        changes[new] = match.group()
        return new

    if isinstance(value, str):
        return _TICKED.sub(replace, value)
    if isinstance(value, list):
        return [rewrite(item, state, prefix, local, changes) for item in value]
    if isinstance(value, dict):
        return {key: rewrite(item, state, prefix, local, changes) for key, item in value.items()}
    return value


def _unused(name, taken):
    while name in taken:
        name += "_"
    return name


def _point(instructions, ref, pointer, rewrote, always):
    """Tell a question which row it's about, unless its instructions already name the row."""
    if rewrote and not always:
        return instructions
    if pointer == "prefix" and isinstance(instructions, str):
        return f"About {ref}: {instructions}"
    if pointer == "scope":
        note = f"Answer only about {ref}. Ignore every other item in the state."
        if isinstance(instructions, dict):
            return {**instructions, _unused("scope", instructions): note}
        return {"question": instructions, "scope": note}
    if isinstance(instructions, dict):
        return {**instructions, _unused("about", instructions): ref}
    return {"question": instructions, "about": ref}


def _packed_question(question, state, prefix, local_extra=()):
    """A question with its paths rewritten, plus whether its instructions changed and what changed."""
    instructions = question.get("instructions")
    local = set(instructions) if isinstance(instructions, dict) else set()
    local |= set(local_extra)
    in_instructions, in_criteria = {}, {}
    out = dict(question)
    out["instructions"] = rewrite(instructions, state, prefix, local, in_instructions)
    if "criteria" in question:
        out["criteria"] = rewrite(question["criteria"], state, prefix, local, in_criteria)
    return out, bool(in_instructions), in_criteria


def row_names(rows, key_names="numbers"):
    """Keys for keyed rows: item_0, item_1, ... or animals shuffled by the batch's first row."""
    if key_names == "numbers":
        return [f"item_{position}" for position in range(len(rows))]
    if key_names != "animals":
        raise ValueError(f"unknown key names {key_names!r}")
    if len(rows) > len(ANIMALS):
        raise ValueError(f"only {len(ANIMALS)} animal names for {len(rows)} rows")
    return random.Random(rows[0]).sample(ANIMALS, len(rows))


def pack(requests, rows, strategy="index", pointer="about", content_key="state", always_point=False,
         lift_state="", lift_question_first=False, lift_point=False, key_names="numbers"):
    """Pack the requests at the given row numbers into one call.

    All of them must use the same model. The result is a Packed whose body is
    an ordinary TypeSafe request. With always_point, the index and keyed
    strategies add a pointer even to questions whose instructions already
    name a path in the row. key_names picks keyed's row keys (see
    row_names). For lift, lift_state is the shared state,
    lift_question_first puts the question before the row's state, and
    lift_point adds "inspect": "`<content_key>`".
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    if pointer not in POINTERS:
        raise ValueError(f"unknown pointer {pointer!r}")
    chosen = [requests[row] for row in rows]
    models = {request.model for request in chosen}
    if len(models) != 1:
        raise ValueError("every packed request must use the same model")
    model = models.pop()

    if strategy == "single":
        if len(chosen) != 1:
            raise ValueError("the single strategy takes one request per call")
        request = chosen[0]
        packed = Packed({"model": model, "state": request.state, "questions": request.questions}, list(rows))
        packed.keys = {key: (0, key) for key in request.questions}
        return packed

    names = row_names(rows, key_names) if strategy == "keyed" else []
    if strategy == "index":
        state = {"items": [request.state for request in chosen]}
    elif strategy == "keyed":
        state = {name: request.state for name, request in zip(names, chosen)}
    else:
        state = lift_state
    packed = Packed({"model": model, "state": state, "questions": {}}, list(rows), names=names)

    for position, request in enumerate(chosen):
        for number, (key, question) in enumerate(request.questions.items()):
            packed_key = f"{position}.{number}"
            if strategy == "lift":
                instructions = question.get("instructions")
                taken = set(instructions) if isinstance(instructions, dict) else {"question"}
                slot = _unused(content_key, taken)
                out, _, criteria_changes = _packed_question(question, request.state, [slot])
                lifted = out["instructions"]
                if not isinstance(lifted, dict):
                    lifted = {"question": lifted}
                if lift_point:
                    lifted = {**lifted, _unused("inspect", lifted): f"`{slot}`"}
                if lift_question_first:
                    out["instructions"] = {**lifted, slot: request.state}
                else:
                    out["instructions"] = {slot: request.state, **lifted}
            else:
                prefix = ["items", position] if strategy == "index" else [names[position]]
                out, rewrote, criteria_changes = _packed_question(question, request.state, prefix)
                ref = f"`{format_path(prefix)}`"
                out["instructions"] = _point(out["instructions"], ref, pointer, rewrote, always_point)
            packed.body["questions"][packed_key] = out
            packed.keys[packed_key] = (position, key)
            if criteria_changes:
                packed.restore[packed_key] = criteria_changes
    return packed


def estimate_tokens(value):
    return len(json.dumps(value, ensure_ascii=False)) / CHARS_PER_TOKEN


def fits(packed):
    """Whether a packed call is likely to stay inside the documented context limits."""
    body = packed.body
    longest = max((estimate_tokens(question) for question in body["questions"].values()), default=0)
    return (
        estimate_tokens(body) <= MAX_REQUEST_TOKENS
        and estimate_tokens(body["state"]) + longest <= MAX_STATE_PLUS_QUESTION_TOKENS
    )


def plan(requests, batches, strategy="index", pointer="about", content_key="state", always_point=False, **options):
    """Pack each batch of row numbers, splitting any batch that might not fit."""
    calls, pending = [], [list(batch) for batch in reversed(batches)]
    while pending:
        rows = pending.pop()
        if strategy == "single":
            calls.extend(pack(requests, [row], "single") for row in rows)
            continue
        packed = pack(requests, rows, strategy, pointer, content_key, always_point, **options)
        if len(rows) > 1 and not fits(packed):
            half = len(rows) // 2
            pending.extend([rows[half:], rows[:half]])
        else:
            calls.append(packed)
    return calls


def _restore(text, changes):
    for new, old in changes.items():
        text = text.replace(new, old)
    return text


def unpack(packed, response):
    """One ordinary TypeSafe response per packed row, in row order.

    Input and output tokens are reported per call, so each row gets an equal
    share; the shares add up to the call's totals.
    """
    count = len(packed.rows)
    usage = response.get("usage") or {}
    out = []
    for position in range(count):
        row_usage = {}
        for name, total in usage.items():
            share, extra = divmod(total, count)
            row_usage[name] = share + (position < extra)
        out.append({"model": response["model"], "answers": {}, "usage": row_usage})
    answers = response["answers"]
    missing = [key for key in packed.keys if key not in answers]
    if missing:
        raise ValueError(f"response is missing {len(missing)} packed answers, such as {missing[0]!r}")
    for key, (position, original) in packed.keys.items():
        answer = answers[key]
        if key in packed.restore and "legend" in answer:
            changes = packed.restore[key]
            answer = {**answer, "legend": {level: _restore(text, changes) for level, text in answer["legend"].items()}}
        out[position]["answers"][original] = answer
    return out
