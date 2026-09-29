"""Compact answer-only Arrow schema. No original prompts or requested model."""

import io
import json

import pyarrow as pa
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer


def required(name, data_type):
    return pa.field(name, data_type, nullable=False)


def probabilities(size):
    return pa.list_(required("item", pa.float64()), size)


class AnswerType(pa.ExtensionType):
    def __init__(self, storage, **metadata):
        self._metadata = json.dumps(
            metadata, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode()
        super().__init__(storage, f"jevaro.{self.kind}")

    def __arrow_ext_serialize__(self):
        return self._metadata

    @classmethod
    def __arrow_ext_deserialize__(cls, storage, serialized):
        metadata = json.loads(serialized)
        result = cls(**metadata)
        if not storage.equals(result.storage_type):
            raise ValueError("Storage does not match Jevaro extension metadata")
        return result

    def __eq__(self, other):
        if not isinstance(other, AnswerType):
            return NotImplemented
        return (type(self) is type(other) and self.storage_type == other.storage_type
                and self._metadata == other._metadata)

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    def __hash__(self):
        return hash((type(self), self.storage_type, self._metadata))

    def array(self, answers):
        storage = pa.array([self.pack(answer) for answer in answers], self.storage_type)
        return pa.ExtensionArray.from_storage(self, storage)


class ChoiceType(AnswerType):
    kind = "choice"

    def __init__(self, labels):
        self.labels = tuple(labels)
        if (not 1 <= len(self.labels) <= 255
                or any(not isinstance(label, str) for label in self.labels)
                or len(set(self.labels)) != len(self.labels)):
            raise ValueError("Choice requires 1 to 255 unique string labels")
        super().__init__(pa.struct([
            required("choice", pa.uint8()),
            required("confidence", pa.float64()),
            required("probabilities", probabilities(len(self.labels))),
        ]), labels=self.labels)

    def pack(self, answer):
        if not isinstance(answer, ChoiceAnswer) or set(answer.probabilities) != set(self.labels):
            raise ValueError("Choice answer does not match the schema")
        return {
            "choice": self.labels.index(answer.choice),
            "confidence": answer.confidence,
            "probabilities": [answer.probabilities[label] for label in self.labels],
        }


class ScoreType(AnswerType):
    kind = "score"

    def __init__(self, legend):
        if not isinstance(legend, (list, tuple)) or not 2 <= len(legend) <= 10:
            raise ValueError("Score requires 2 to 10 levels")
        super().__init__(pa.struct([
            required("score", pa.float64()),
            required("confidence", pa.float64()),
            required("probabilities", probabilities(len(legend))),
        ]), legend=legend)

    def pack(self, answer):
        legend = dict(enumerate(json.loads(self._metadata)["legend"]))
        if (not isinstance(answer, ScoreAnswer) or answer.legend != legend
                or set(answer.probabilities) != set(legend)):
            raise ValueError("Score answer does not match the schema")
        return {
            "score": answer.score,
            "confidence": answer.confidence,
            "probabilities": [answer.probabilities[level] for level in legend],
        }


class NoulType(AnswerType):
    kind = "noul"

    def __init__(self):
        super().__init__(pa.float64())

    def pack(self, answer):
        if not isinstance(answer, NoulAnswer):
            raise ValueError("Expected a Noul answer")
        return answer.noul


def schema_for(questions):
    def answer_type(question):
        if isinstance(question, Choice):
            return ChoiceType(question.criteria)
        if isinstance(question, Score):
            return ScoreType(question.criteria)
        if isinstance(question, Noul):
            return NoulType()
        raise TypeError("Unsupported question type")

    return pa.schema([required(name, answer_type(q)) for name, q in questions.items()])


def answer_batch(schema, responses):
    if any(set(response.answers) != set(schema.names) for response in responses):
        raise ValueError("Response answer names do not match the questions")
    return pa.RecordBatch.from_arrays(
        [field.type.array([response.answers[field.name] for response in responses])
         for field in schema], schema=schema,
    )


class StreamSink(io.RawIOBase):
    """Keep only the bytes since the last HTTP chunk, not the whole IPC stream."""

    def __init__(self):
        super().__init__()
        self._chunks = []
        self._position = 0

    def writable(self):
        return True

    def tell(self):
        return self._position

    def write(self, data):
        data = bytes(data)
        self._chunks.append(data)
        self._position += len(data)
        return len(data)

    def drain(self):
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data
