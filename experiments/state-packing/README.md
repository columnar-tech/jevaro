# Packing many states into one TypeSafe call

The TypeSafe API evaluates one state per request. These experiments test ways
to evaluate many independent states in one call, using only the existing API,
so the calls work with any client or SDK. The goal is an optional Jevaro
feature that raises throughput. Everything here is standalone JSON; nothing in
Jevaro changed.

All runs used `jev-1.13.0` on 2026-10-05. The main comparisons use 10,000-row
runs. Variants were first screened in 320-row pilots.

## Summary

- **Only one approach keeps each row's answers independent of the other rows:
  "lift".** Each packed question carries its own row's state in its
  `instructions`, and the request's `state` is `""`. Rows can't affect each
  other, because questions in a request are evaluated independently.
- **Lift raised throughput up to 12× and cut cost 28% on short messages:**
  6,514 rows/s with 64 rows per call and 64 calls in flight, against 540 rows/s
  for one row per call at 100 calls in flight. That run lasted only 1.5 s, so
  it is a burst figure, not a sustained one.
- **Lift shifts answers a little, the same way every time.** Moving a row's
  state into the instructions changes some answers. On messages, urgency
  scores are 0.03 lower on average. The shift is 2–4× the run-to-run noise of
  one-row-per-call. It doesn't depend on how many rows share a call, what
  they contain, or where a row sits. Two lift runs agree with each other as
  closely as two native runs do.
- **Lift repeats a row's state once per question,** so it costs more when
  states are long and there are many questions. On structured tickets with 6
  questions it used 1.8× the tokens of one-row-per-call and was barely faster.
- **Putting many rows in one shared state doesn't work.**
  - *Index addressing* (`` `items[17]` ``): Jev can't count positions.
    Agreement falls from 92% for the first 8 rows to 38% for rows 24–31.
  - *Keyed addressing* (`` `item_17` ``): stays at about 94–95% agreement.
    Every answer gets blurrier when other rows are present, even with only 8
    rows per call.
  - *Keyed with animal names* (`` `walrus` ``): no better, at 94.5%
    agreement. Most misses don't match any other row's answer, so the problem
    is blur rather than mixing rows up, and better names can't fix it.
- **Keyed packing can still look fine.** Its changes fall almost entirely on
  rows where Jev is unsure, so spot checks look right, and totals move by only
  about 2 percentage points. But a row's answer depends on which other rows
  share its call. The same row run with different neighbours gave a different
  department 5.3% of the time, against 0.6–0.9% for one row per call or lift.

## The approaches

Take two requests with the same question:

```json
{"model": "jev-latest", "state": "The shoes don't fit. Please refund my money today.",
 "questions": {"refund": {"type": "noul", "instructions": "Is the customer requesting a refund?"}}}
{"model": "jev-latest", "state": "Where is my parcel? It's a week late.",
 "questions": {"refund": {"type": "noul", "instructions": "Is the customer requesting a refund?"}}}
```

Each strategy turns them into one ordinary request.

- **Question keys are new and opaque,** such as `0.0` for row 0, question 0.
  The API reference says keys are never sent to the model.
- **Unpacking is a lookup.** `unpack()` maps answer `1.0` back to row 1's
  `refund`.
- **Answers are passed through unchanged:** the type, probabilities,
  confidence and legend.

**Lift.** Each question carries its row's state; the shared state is empty:

```json
{"model": "jev-latest", "state": "", "questions": {
  "0.0": {"type": "noul", "instructions": {"state": "The shoes don't fit. Please refund my money today.", "question": "Is the customer requesting a refund?"}},
  "1.0": {"type": "noul", "instructions": {"state": "Where is my parcel? It's a week late.", "question": "Is the customer requesting a refund?"}}}}
```

**Keyed.** The rows share the state, and each question points at its row:

```json
{"model": "jev-latest", "state": {"item_0": "The shoes don't fit. Please refund my money today.", "item_1": "Where is my parcel? It's a week late."},
 "questions": {
  "0.0": {"type": "noul", "instructions": {"question": "Is the customer requesting a refund?", "about": "`item_0`"}},
  "1.0": {"type": "noul", "instructions": {"question": "Is the customer requesting a refund?", "about": "`item_1`"}}}}
```

With `key_names="animals"`, keyed names each row after a different animal,
shuffled per call: `{"orca": ..., "pelican": ...}` with pointers such as
`` `pelican` ``.

**Index.** The same as keyed, with `{"items": [...]}` and pointers such as
`` `items[1]` ``. This follows the docs' counting example, which asks one
question per item about `` `items[i]` ``.

**Paths into the state are rewritten.** A question can name part of its state
with a backticked path, as the docs recommend. The packer rewrites a path only
when it resolves against that row's state. A backticked name that belongs to
the instructions object, such as `policy` below, is left alone. For this row:

```json
{"ticket": {"messages": [{"from": "customer", "text": "The vase arrived cracked. Please refund it."}]}, "order": {"status": "delivered"}}
```

this question:

```json
{"type": "noul", "instructions": {
  "policy": "Unused items can be returned within 30 days of delivery for a full refund.",
  "question": "Is what the customer asks for in `ticket.messages[0].text` covered by `policy`?"}}
```

is packed as follows:

- **Lift:** `` {"state": {...the row...}, "policy": "...", "question": "Is what the customer asks for in `state.ticket.messages[0].text` covered by `policy`?"} ``
- **Keyed (row 1):** `` {"policy": "...", "question": "Is what the customer asks for in `item_1.ticket.messages[0].text` covered by `policy`?"} ``

Keyed and index add an `about` pointer only when the instructions don't
already name a path in the row. Other rules:

- **Paths inside criteria are rewritten too.**
- **Choice option names are never changed,** because they're dict keys.
- **Score legends are restored:** when the API echoes rewritten criteria back
  in a legend, unpacking puts the original text back.

## Method

**Workloads.** Both use the same 10,000 synthetic customer messages as
Jevaro's live benchmarks. `corpus.py` is a copy of that generator and
reproduces `states.txt` exactly.

- **`messages`:** each state is a plain string. The questions are the
  benchmark's: a Choice (`department`), a Score (`urgency`), and a Noul
  (`refund`).
- **`tickets`:** each state is an object with a two-message ticket, a
  customer record, and an order. Six questions cover each way a question can
  refer to its state:
  - a nested path: `` `ticket.messages[0].text` ``
  - two paths in one question
  - a path inside structured instructions
  - structured Choice, Score and Noul criteria
  - no path at all (`tone`)
  - a backticked field of the instructions object rather than the state
    (`policy`)

**Reference.** A fresh one-row-per-call run of each workload. The noise floor
comes from comparing one-row-per-call runs with each other. For messages, that
was two earlier runs (2026-09-28 and 2026-10-05) and a run spread over 4
connections. For tickets, it was a 320-row repeat.

**Metrics** (`compare.py`), per question:

- **Choice:** agreement on the top choice, and the mean total variation
  distance (TVD) between the two probability distributions.
- **Score:** agreement on the rounded level, and the mean absolute difference
  in score (range 0–2).
- **Noul:** agreement at 0.5, and the mean absolute difference in probability.

"Off by ≥ 0.25" counts answers whose distance is at least 0.25.

**Batching against type.** Hostile batching tests isolation. Rows are sorted
by their reference department, and each call gets 28 rows with mostly the same
department plus 4 from elsewhere. If rows leak into each other, the 4
outsiders should drift toward the majority's department.

**Authoring metadata.** The corpus records the scenario and deadline style
each message was written from. `compare.py --labels` scores department and
urgency against them. The generator says these aren't ground truth, so this is
only a rough check that answers didn't get worse.

## Results

### Fidelity on messages (10,000 rows, against a fresh one-row-per-call run)

| Run | Department agree / TVD | Urgency agree / diff | Refund agree / diff | Off by ≥ 0.25 |
| --- | --- | --- | --- | --- |
| One row per call, 2026-10-05 Jevaro run | 99.58% / 0.006 | 99.26% / 0.009 | 99.73% / 0.005 | 0 |
| One row per call, 2026-09-28 run | 99.57% / 0.006 | 99.26% / 0.009 | 99.81% / 0.005 | 0 |
| One row per call, 4 connections | 99.49% / 0.006 | 99.28% / 0.009 | 99.69% / 0.005 | 1 |
| **Lift, 32 rows per call** | 98.44% / 0.015 | 97.31% / 0.033 | 99.26% / 0.015 | 315 |
| **Lift, 64 rows per call** | 98.50% / 0.015 | 97.35% / 0.033 | 99.21% / 0.015 | 322 |
| **Lift, 32, batched against type** | 98.53% / 0.015 | 97.25% / 0.033 | 99.21% / 0.015 | 322 |
| Keyed, 32, batched against type | 95.22% / 0.055 | 94.24% / 0.087 | 94.50% / 0.059 | 2,389 |
| Keyed with animal names, 32, batched against type | 94.48% / 0.061 | 94.27% / 0.084 | 94.59% / 0.059 | 2,442 |

- **Lift runs agree with each other at the noise floor.** Lift at 32 rows per
  call against lift batched against type, or against lift at 64: department
  99.38–99.50% (TVD 0.006), urgency 99.48–99.62% (diff 0.007), refund
  99.71–99.73% (diff 0.005).
- **Batching against type didn't change lift.** The 1,277 rows whose
  department differed from their call's majority agreed 98.59% of the time.
  Their probability for the majority's department moved by only +0.003.
- **Keyed did lose fidelity, but not by copying neighbours.** Its outsider
  rows didn't move toward the majority either (+0.004). Instead, the majority
  rows became less sure of their own department (−0.031). With animal
  names, the figures were +0.005 and −0.040.
- **Animal names carry no meaning into the answers.** Across the 66 animals,
  about 150 rows each, per-animal averages varied no more than random groups
  of the same sizes. That held for urgency shift, refund shift and department
  misses (p = 0.45–0.68).
- **Keyed misses are blur, not mix-ups.** Of 897 department answers off by
  0.25 or more with animal names, 784 matched no other row's reference answer
  in the same call.

### Fidelity on tickets (10,000 rows)

| Question | Noise: one row per call, repeated (320 rows) | Lift (16 rows per call) |
| --- | --- | --- |
| department | 98.75% / 0.007 | 98.50% / 0.010 |
| urgency | 99.69% / 0.006 | 99.50% / 0.010 |
| refund | 100.00% / 0.004 | 99.69% / 0.006 |
| item_matches (two paths) | 100.00% / 0.003 | 99.98% / 0.005 |
| tone (no path) | 99.38% / 0.009 | 98.55% / 0.015 |
| policy_covers (an instructions field) | 100.00% / 0.008 | 96.90% / 0.028 |

The 320-row pilots on tickets showed the same failures as on messages:

- **Keyed:** 91–98% agreement with numbered keys, and 90–99.7% with animal
  names.
- **Index:** 39–84% agreement.

Naming explicit paths in the questions didn't help either of them.

### Why shared state fails

Department agreement by position in a 32-row call (320-row pilots, messages):

| Position | 0–7 | 8–15 | 16–23 | 24–31 |
| --- | --- | --- | --- | --- |
| Index | 92.5% | 87.5% | 50.0% | 37.5% |
| Keyed | 97.5% | 91.3% | 95.0% | 91.3% |
| Keyed, animal names | 95.0% | 88.8% | 95.0% | 88.8% |
| Lift | 100.0% | 96.3% | 97.5% | 96.3% |

- **The pointer wording didn't matter for index.** `about`, a prefix, and an
  explicit "ignore the other items" note all scored 67–68% on department.
- **Fewer rows didn't help keyed.** At 8 rows per call it was no better
  (93.1%) than at 32. The jaggedness page warns that unrelated content in the
  state reduces accuracy, and even 7 other rows was enough to show it.

### Why keyed packing can still look fine

Its changes fall where Jev was unsure anyway. This table shows department
agreement with one row per call, grouped by how confident the one-row-per-call
answer was (10,000 rows; the packed runs were batched against type):

| Top probability, one row per call | Rows | Another one-row-per-call run | Lift | Keyed | Keyed, animal names |
| --- | --- | --- | --- | --- | --- |
| 0.95 or higher | 8,008 | 100.0% | 100.0% | 99.8% | 99.6% |
| 0.80–0.95 | 1,065 | 100.0% | 99.9% | 89.9% | 87.2% |
| Below 0.80 | 927 | 95.5% | 84.2% | 61.5% | 58.4% |

On the 80% of rows where Jev is at least 95% sure, keyed changes almost
nothing. Most of its changes land on borderline rows, where either answer
looks plausible, so checking a sample of answers won't reveal them. Lift's
smaller shift is concentrated on the same rows.

Totals barely move either:

| Run (10,000 rows) | returns | shipping | billing | other | Mean urgency | Refund at ≥ 0.5 |
| --- | --- | --- | --- | --- | --- | --- |
| One row per call | 37.3% | 23.4% | 18.1% | 21.2% | 0.700 | 25.1% |
| Another one-row-per-call run | 37.3% | 23.3% | 18.2% | 21.2% | 0.701 | 25.2% |
| Lift (against type) | 37.3% | 22.5% | 18.3% | 21.9% | 0.670 | 25.6% |
| Keyed (against type) | 35.2% | 23.8% | 18.8% | 22.2% | 0.622 | 24.4% |
| Keyed, animal names (against type) | 35.2% | 23.5% | 19.1% | 22.1% | 0.629 | 23.9% |

On top of that, keyed matched the authoring metadata as well as one row per
call did, or better (see below). On a spot check, the totals, or that rough
accuracy check, keyed packing looks fine.

### Keyed answers depend on the batch

This compares the same 320 rows run twice with the same method, sharing calls
with different neighbours each time: once in input order (the pilot) and once
batched against type. The one-row-per-call row compares two independent runs.

| Method | Department agreement | Urgency diff | Refund diff | Off by ≥ 0.25 |
| --- | --- | --- | --- | --- |
| One row per call | 99.38% | 0.009 | 0.004 | 0 |
| Lift | 99.06% | 0.007 | 0.004 | 0 |
| Keyed | 94.69% | 0.030 | 0.049 | 40 |
| Keyed, animal names | 94.69% | 0.036 | 0.048 | 48 |

With keyed packing, a row's answer depends on which other rows happen to share
its call. The same row can get a different answer when the table is sorted or
filtered differently, or batched at another size. Lift answers, like
one-row-per-call answers, don't depend on the other rows.

### Why lift shifts answers

The shift comes from moving the state into the instructions, not from packing.

| 320-row pilot (messages) | Urgency mean shift | Urgency diff | Department TVD | Refund diff |
| --- | --- | --- | --- | --- |
| One row per call, question wrapped as `{"question": ...}` | −0.004 | 0.014 | 0.011 | 0.008 |
| Lift, 1 row per call | −0.031 | 0.034 | 0.017 | 0.014 |
| Lift, 8 | −0.031 | 0.034 | 0.018 | 0.013 |
| Lift, 32 | −0.032 | 0.034 | 0.017 | 0.014 |
| Lift, 128 | −0.031 | 0.033 | 0.018 | 0.014 |
| Lift, row's state field named `content` / `text` / `input` | −0.036 / −0.033 / −0.030 | 0.035–0.038 | 0.015–0.017 | 0.011 |
| Lift, plus `` "inspect": "`state`" `` | −0.028 | 0.031 | 0.017 | 0.013 |
| Lift, question before the state | −0.036 | 0.042 | 0.025 | 0.048 |
| Lift, shared state explains the layout | −0.047 | 0.048 | 0.024 | 0.016 |

- **Lift with one row per call shifts as much as lift with 128.** So packing
  adds nothing; the format causes the shift.
- **Wrapping the question in an object alone stays near the noise floor.**
- **None of the formats removed the shift.** The default puts the row's state
  first, under `state`, followed by the question. It's simple and as good as
  any of the alternatives.
- **The shift is in one direction.** At 10,000 rows, 292 of the 297 urgency
  answers that moved by 0.25 or more went down. The most common department
  change was `shipping` to `other`.

**Authoring metadata check (10,000 rows).**

| Workload and run | Department | Urgency |
| --- | --- | --- |
| Messages: one row per call → lift | 93.59% → 92.90% | 94.36% → 96.50% |
| Tickets: one row per call → lift | 93.93% → 93.79% | 97.90% → 97.96% |
| Messages: keyed, batched against type | 94.70% | 99.36% |

Keyed's urgency matched the metadata better even while its agreement with
one-row-per-call fell. So "different" doesn't necessarily mean "worse". For a
proxy that's meant to be transparent, though, the test is the same answers as
unpacked calls.

### Throughput and cost

| Run | Rows per call | Calls in flight | Connections | Rows/s | Tokens per row | Tokens/s | p50 latency | Cost per 10k rows | 429s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Messages, one row per call | 1 | 100 | 1 | 540 | 475 | 257k | 168 ms | $0.20 | 0 |
| Messages, one row per call | 1 | 400 | 4 | 954 | 475 | 453k | 378 ms | $0.20 | 0 |
| Messages, lift | 32 | 16 | 1 | 2,041 | 344 | 701k | 230 ms | $0.14 | 0 |
| Messages, lift | 64 | 64 | 1 | 6,514 | 340 | 2.21M | 471 ms | $0.14 | 0 |
| Messages, keyed (against type) | 32 | 16 | 1 | 2,049 | 290 | 594k | 233 ms | $0.12 | 0 |
| Messages, keyed with animals (against type) | 32 | 16 | 1 | 2,000 | 284 | 569k | 236 ms | $0.12 | 0 |
| Tickets, one row per call | 1 | 100 | 1 | 527 | 1,053 | 555k | 170 ms | $0.44 | 0 |
| Tickets, lift | 16 | 16 | 1 | 646 | 1,892 | 1.22M | 350 ms | $0.79 | 6 |

Notes:

- **Tickets with lift used 16 rows per call.** The packer halved the 32-row
  batches to stay under its conservative token estimate. They came to about
  31k tokens each.
- **The one-row-per-call runs each lost one connection** (99 and 94 calls,
  retried). The 2026-09-29 benchmarks saw this too. No packed run lost one.

**Token accounting** (probe calls):

- **Every call has a fixed overhead of about 259 input tokens.** On top of
  that, each message state is about 42 tokens, and the three questions are 96,
  66 and 14 tokens.
- **So per row:**
  - one row per call costs about 259 + s + Σq tokens;
  - lift costs about K·s + Σq + 259/N, plus a few tokens of wrapping per
    question.

  Here s is the state's tokens, Σq is all of a row's questions, K is the
  number of questions, and N is rows per call.
- **When lift is cheaper:** when (K − 1)·s stays below about 250 tokens. That
  holds for short states and few questions: messages have K = 3 and s ≈ 42.
  It fails for long states with many questions: tickets have K = 6.
- **Keyed and index store each state once,** so they are always the
  cheapest, but their answers are unusable.

**Rate limits.**

- **Documented:** 80 requests/s and 100k tokens/s, "adjusting dynamically".
- **Observed:** no 429s at 2.2M tokens/s on messages, but six at 1.2M
  tokens/s on tickets, which used larger calls. Retries recovered all six.
- **Tokens per second, not requests, is the ceiling to plan for.** So lift
  adds the most throughput where it also cuts tokens per row.
- **No rate-limit headers are returned.**

## Recommendations for an optional Jevaro feature

1. **Offer lift and nothing else, off by default.** Index and keyed change
   too many answers. They also make each row's answer depend on the other
   rows in its call.
2. **Pack (row, question) pairs, not rows.** Each lifted question is
   self-contained, so a row's questions can be spread across calls. A row
   then fits whenever its state plus its longest question fits in 32k tokens,
   the same limit as today. `packing.py` packs whole rows, and that's enough
   for these experiments.
3. **Stay inside the documented limits:** 64k tokens per call, and 32k for the
   state plus the longest question. Estimate conservatively, and split the
   call if TypeSafe rejects it (`run.py` halves a call on 400, 413 or 422).
   One call held 384 questions (128 rows × 3) without trouble.
4. **Only pack rows that use the same model.** Questions can differ by row.
5. **Use the path rules from `packing.py`:**
   - rewrite only backticked paths that resolve in the row;
   - leave instruction fields, Choice option names and anything else alone;
   - restore Score legends.
6. **A row is complete when all its questions are back,** so Jevaro's ordered
   streaming can stay row-based.
7. **Split `usage` across the rows of a call,** because TypeSafe reports it
   per call.
8. **Make concurrency adapt to 429s.** Packed calls are large, so each retry
   costs more, and tokens per second is the limit that applies.
9. **Warn or fall back to one row per call** when (K − 1)·s is more than about
   250 tokens. There, lift costs more per row, and under a token limit it is
   also slower.
10. **Tell users what to expect.** Packed answers are consistent from run to
    run but differ slightly from unpacked ones. Don't mix the two in one
    analysis, and re-check any thresholds tuned on unpacked answers.

## Files

| File | Purpose |
| --- | --- |
| `packing.py` | Pack requests into calls (`single`, `index`, `keyed` with numbered or animal keys, `lift`) and unpack responses |
| `workloads.py` | The `messages` and `tickets` workloads, plus a `messages_wrapped` diagnostic |
| `corpus.py` | The synthetic message corpus |
| `run.py` | Run a workload live; writes `results/<label>.jsonl` and `.json` (gitignored) |
| `compare.py` | Agreement and distances, broken down by position, the reference's confidence, and batching against type; totals; and authoring metadata |
| `test_packing.py` | Offline tests: `python -m unittest test_packing` |
| `findings/` | The comparisons behind the tables above, with each run's timing and token counts; `batch-neighbours/` holds the batch-dependence comparisons |

Runs are billed. `--dry-run` prints the first call and the estimated tokens
without calling the API.

```sh
export TYPESAFE_API_KEY=...
uv run run.py messages --strategy single --concurrency 100 --label messages_single
uv run run.py messages --strategy lift --batch 64 --concurrency 64 --label messages_lift_b64_c64
uv run run.py messages --strategy keyed --order against-type --reference results/messages_single.jsonl --label keyed_against_type
uv run run.py messages --strategy keyed --rows 320 --concurrency 10 --label keyed_pilot
python compare.py results/messages_single.jsonl results/messages_lift_b64_c64.jsonl --labels --positions --confidence --totals
python compare.py results/keyed_pilot.jsonl results/keyed_against_type.jsonl --rows 320  # Batch dependence
```

## Caveats

- **One corpus and one model.** The corpus is synthetic English support
  messages, and every run used `jev-1.13.0` on one day. The shift might
  differ for other content or a later model.
- **Throughput figures are short bursts.** A packed 10,000-row run lasted
  1.5–5 seconds. They show what one connection can carry, not a sustained
  rate, and rate limits may change.
- **Agreement with one-row-per-call is the criterion, not accuracy.** The
  authoring metadata is only a rough check.
- **Not tested:**
  - states that are arrays;
  - states close to the context limit;
  - non-English text;
  - `jev-preview`;
  - Arrow input;
  - running inside Jevaro.
