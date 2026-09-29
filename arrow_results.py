"""Lossless Arrow answer types for the fixed questions in all_types.py.

Import this module before reading an IPC stream to register the extension types.
The JSON extension metadata lives on each field in the serialized Arrow schema.
"""

import json
from collections.abc import Iterable
from itertools import islice

import pyarrow as pa
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer, TypeSafeClient

from all_types import QUESTIONS
from request_scheduler import (
    AdaptiveRateLimiter, DEFAULT_CONCURRENCY, DEFAULT_MAX_RETRIES,
    evaluate_chunk, validate_count,
)


def required(name, data_type):
    return pa.field(name, data_type, nullable=False)


def probability_vector(size):
    # Fixed-size lists have no offsets; the element itself is also non-nullable.
    return pa.list_(required("item", pa.float64()), size)


class AnswerScalar(pa.ExtensionScalar):
    def as_py(self, **kwargs):
        if not self.is_valid:
            return None
        return self.type.unpack(self.value.as_py(**kwargs))


class AnswerType(pa.ExtensionType):
    """Freeze only the shared answer metadata needed to decode the storage."""

    def __init__(self, storage_type, **metadata):
        self._serialized = json.dumps(
            {"version": 2, **metadata},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        super().__init__(storage_type, f"jev_demo.{self.kind}")

    @property
    def metadata(self):
        # Return a copy, keeping extension types immutable.
        return json.loads(self._serialized)

    def __arrow_ext_serialize__(self):
        return self._serialized

    def __eq__(self, other):
        if not isinstance(other, pa.ExtensionType):
            return NotImplemented
        return (
            type(self) is type(other)
            and self.storage_type == other.storage_type
            and self._serialized == other.__arrow_ext_serialize__()
        )

    def __hash__(self):
        return hash((type(self), self.storage_type, self._serialized))

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type, serialized):
        metadata = json.loads(serialized)
        if metadata.get("version") != 2:
            raise ValueError("Unsupported Jev extension metadata version")
        try:
            result = cls(**{key: value for key, value in metadata.items() if key != "version"})
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid Jev answer metadata") from error
        if not result.storage_type.equals(storage_type) or result.metadata != metadata:
            raise ValueError("Jev extension storage does not match the answer metadata")
        return result

    def __arrow_ext_scalar_class__(self):
        return AnswerScalar

    def array(self, answers):
        storage = pa.array([self.pack(a) for a in answers], type=self.storage_type)
        return pa.ExtensionArray.from_storage(self, storage)


class ChoiceType(AnswerType):
    kind = "choice"

    def __init__(self, labels):
        if isinstance(labels, (str, bytes)):
            raise ValueError("Choice labels must be a sequence of strings")
        self._labels = tuple(labels)
        if (not 1 <= len(self._labels) <= 255
                or any(not isinstance(label, str) for label in self._labels)
                or len(set(self._labels)) != len(self._labels)):
            raise ValueError("Choice requires 1 to 255 unique string labels")
        storage = pa.struct([
            required("choice", pa.uint8()),
            required("confidence", pa.float64()),
            required("probabilities", probability_vector(len(self._labels))),
        ])
        super().__init__(storage, labels=self._labels)

    def pack(self, answer):
        if not isinstance(answer, ChoiceAnswer):
            raise TypeError("Expected a ChoiceAnswer")
        if set(answer.probabilities) != set(self._labels):
            raise ValueError("Choice probability keys differ from the question's labels")
        # Do not infer choice or confidence from probabilities: preserve what was returned.
        return {
            "choice": self._labels.index(answer.choice),
            "confidence": answer.confidence,
            "probabilities": [answer.probabilities[label] for label in self._labels],
        }

    def unpack(self, value):
        return {
            "type": "choice",
            "choice": self._labels[value["choice"]],
            "confidence": value["confidence"],
            "probabilities": dict(zip(self._labels, value["probabilities"], strict=True)),
        }


class ScoreType(AnswerType):
    kind = "score"

    def __init__(self, legend):
        if not isinstance(legend, (list, tuple)) or not 2 <= len(legend) <= 10:
            raise ValueError("Score requires 2 to 10 levels")
        storage = pa.struct([
            required("score", pa.float64()),
            required("confidence", pa.float64()),
            required("probabilities", probability_vector(len(legend))),
        ])
        super().__init__(storage, legend=legend)

    @property
    def legend(self):
        return dict(enumerate(self.metadata["legend"]))

    def pack(self, answer):
        if not isinstance(answer, ScoreAnswer):
            raise TypeError("Expected a ScoreAnswer")
        legend = self.legend
        if answer.legend != legend:
            raise ValueError("Score legend changed; it cannot share this schema")
        if set(answer.probabilities) != set(legend):
            raise ValueError("Score probability keys differ from the question's levels")
        return {
            "score": answer.score,
            "confidence": answer.confidence,
            "probabilities": [answer.probabilities[level] for level in legend],
        }

    def unpack(self, value):
        legend = self.legend
        return {
            "type": "score",
            "score": value["score"],
            "confidence": value["confidence"],
            "legend": legend,
            "probabilities": dict(zip(legend, value["probabilities"], strict=True)),
        }


class NoulType(AnswerType):
    kind = "noul"

    def __init__(self):
        # Only a probability varies per row; a one-member struct adds no meaning.
        super().__init__(pa.float64())

    def pack(self, answer):
        if not isinstance(answer, NoulAnswer):
            raise TypeError("Expected a NoulAnswer")
        return answer.noul

    def unpack(self, value):
        return {"type": "noul", "noul": value}


def type_for_question(question):
    """Retain only labels and the expected answer legend, never the prompt."""
    if isinstance(question, Choice):
        return ChoiceType(question.criteria)
    if isinstance(question, Score):
        return ScoreType(question.criteria)
    if isinstance(question, Noul):
        return NoulType()
    raise TypeError(f"Unsupported question type: {type(question).__name__}")

# Registration is by extension name, not by a particular set of labels/levels.
for _question in QUESTIONS.values():
    pa.register_extension_type(type_for_question(_question))


def evaluate_states(
    states: Iterable[str],
    client: TypeSafeClient,
    *,
    batch_size: int = 128,
    model: str = "jev-latest",
    concurrency: int = DEFAULT_CONCURRENCY,
    max_retries: int = DEFAULT_MAX_RETRIES,
    limiter: AdaptiveRateLimiter | None = None,
) -> pa.RecordBatchReader:
    """Return a lazy reader using bounded parallel calls with shared rate control.

    Keep the supplied client open while consuming the reader. At most batch_size
    responses are buffered, in input order, with at most concurrency calls in flight.
    No work runs between Arrow batches. Retries use the same shared limiter;
    exhausted retries and permanent errors propagate without fabricated/null rows.
    Pass a limiter to customize limits and inspect attempts, usage, and learned rate.
    Per-request token usage and transport metadata are outside the answer table.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    if isinstance(states, (str, bytes)):
        raise TypeError("states must be an iterable of messages, not a single string")
    validate_count(concurrency, "concurrency")
    validate_count(max_retries, "max_retries", minimum=0)
    if limiter is None:
        limiter = AdaptiveRateLimiter()
    questions = {name: question.model_copy(deep=True) for name, question in QUESTIONS.items()}
    types = {name: type_for_question(q) for name, q in questions.items()}
    schema = pa.schema(
        [required("state", pa.string()), *(required(name, t) for name, t in types.items())],
    )

    def batches():
        iterator = iter(states)
        while chunk := list(islice(iterator, batch_size)):
            if any(not isinstance(state, str) or not state.strip() for state in chunk):
                raise ValueError("Every state must be a nonempty string")
            responses = evaluate_chunk(
                chunk, client, questions, model, limiter,
                concurrency=concurrency, max_retries=max_retries,
            )
            answers = {name: [] for name in questions}
            for response in responses:
                if set(response.answers) != set(questions):
                    raise ValueError("Response answer names do not match the questions")
                for name in questions:
                    answers[name].append(response.answers[name])
            columns = [pa.array(chunk, type=pa.string())]
            columns.extend(t.array(answers[name]) for name, t in types.items())
            batch = pa.RecordBatch.from_arrays(columns, schema=schema)
            batch.validate(full=True)
            yield batch

    return pa.RecordBatchReader.from_batches(schema, batches())
