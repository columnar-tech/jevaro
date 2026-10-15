"""Offline test fixture: real TypeSafe SDK over a mocked upstream transport."""

import asyncio
import json
import threading
from collections import Counter
from dataclasses import replace
from email.parser import BytesParser
from email.policy import HTTP

import httpx2
from typesafe_sdk import AsyncTypeSafeClient

from jevaro_server.app import create_app

try:
    from compression.zstd import decompress
except ImportError:  # Before Python 3.14, the SDK compresses only with backports.zstd installed.
    try:
        from backports.zstd import decompress
    except ImportError:
        decompress = None


def read_body(request):
    """The JSON request and {asset reference: image bytes}, from JSON or a multipart upload."""
    content = request.content
    if request.headers.get("content-encoding") == "zstd":
        content = decompress(content)
    content_type = request.headers["content-type"]
    if not content_type.startswith("multipart/form-data"):
        return json.loads(content), {}
    message = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + content)
    parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
             for part in message.iter_parts()}
    payload = json.loads(parts.pop("request"))
    images = {reference: parts.pop(asset["part"]) for reference, asset in payload["assets"].items()}
    assert not parts, f"Unclaimed parts: {list(parts)}"
    return payload, images


class Upstream:
    def __init__(self):
        self.started = []
        self.finished = []
        self.cancelled = []
        self.attempts = Counter()
        self.active = 0
        self.peak = 0
        self.auth_matches = []
        self.http_clients = []
        self.payloads = []
        # Per call: its danger-zone headers, Content-Encoding, and {reference: image bytes}.
        self.headers = []
        self.encodings = []
        self.images = []
        self.gate = threading.Event()
        self.transport = httpx2.MockTransport(self.handle)

    def client(self, *, retry, **kwargs):
        self.http_clients.append(kwargs["http_client"])
        # Keep the server's retry count; skip real backoff delays.
        return AsyncTypeSafeClient(
            **kwargs, retry=replace(retry, backoff_initial=0.001, backoff_jitter=0),
        )

    async def handle(self, request):
        payload, images = read_body(request)
        self.payloads.append(payload)
        self.images.append(images)
        self.headers.append({name: value for name, value in request.headers.items()
                             if name.startswith("typesafe-danger-zone-")})
        self.encodings.append(request.headers.get("content-encoding"))
        state = payload["state"]
        # A packed call has an empty state; each question carries its row's state,
        # and its key starts with the row's position in the call.
        questions = payload["questions"]
        packed = state == "" and all(
            isinstance(q.get("instructions"), dict) and "state" in q["instructions"] for q in questions.values())
        if packed:
            rows = {}
            for name, question in questions.items():
                rows.setdefault(name.split(".", 1)[0], question["instructions"]["state"])
            row_of = {name: rows[name.split(".", 1)[0]] for name in questions}
            states = list(rows.values())
        else:
            row_of = {name: state for name in questions}
            states = [state]
        configs = [row if isinstance(row, dict) else {} for row in states]
        config = configs[0]
        number = config.get("id", 0)
        self.started.extend(states)
        self.auth_matches.append(request.headers.get("Authorization") == "Bearer jevaro-test-key")
        self.attempts[number] += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if any(c.get("gate") for c in configs):
                while not self.gate.is_set():
                    await asyncio.sleep(0.005)
            await asyncio.sleep(max(c.get("delay", 0) for c in configs))
            statuses = next((c["statuses"] for c in configs if c.get("statuses")), [])
            attempt = self.attempts[number] - 1
            if attempt < len(statuses):
                return httpx2.Response(statuses[attempt], json={"error": "test failure"},
                                       headers={"retry-after-ms": "1"})
            limit = min((c["max_questions"] for c in configs if "max_questions" in c), default=None)
            if limit is not None and len(questions) > limit:
                return httpx2.Response(422, json={"error": "too many questions"})
            answers = {}
            for name, question in questions.items():
                kind = question["type"]
                row = row_of[name]
                number = row.get("id", 0) if isinstance(row, dict) else 0
                if kind == "noul":
                    answers[name] = {"type": kind, "noul": (number % 1000) / 1000}
                elif kind == "box":
                    # Edges from the row's id, so each row's box is distinct and easy to check.
                    answers[name] = {"type": kind, "box": {
                        "top": number / 1000, "bottom": 0.5 + number / 1000,
                        "left": 0.25 + number / 1000, "right": 0.75 + number / 1000,
                    }, "confidence": None if isinstance(row, dict) and row.get("no_confidence") else 0.875}
                elif kind == "choice":
                    labels = list(question["criteria"])
                    winner = labels[number % len(labels)]
                    answers[name] = {
                        "type": kind, "choice": winner, "confidence": 0.12345678901234567,
                        "probabilities": {label: float(label == winner) for label in labels},
                    }
                else:
                    levels = question["criteria"]
                    answers[name] = {
                        "type": kind, "score": 0.9876543210987654,
                        "confidence": 0.23456789012345678,
                        "legend": dict(enumerate(levels)),
                        "probabilities": {str(i): 1 / len(levels) for i in range(len(levels))},
                    }
            self.finished.append(config.get("id", 0))
            return httpx2.Response(200, json={
                "answers": answers, "model": payload["model"],
                "usage": {"input_tokens": 42, "output_tokens": 4},
            })
        except asyncio.CancelledError:
            self.cancelled.append(config.get("id", 0))
            raise
        finally:
            self.active -= 1


upstream = Upstream()
app = create_app(client_factory=upstream.client, transport=upstream.transport, concurrency=3)


@app.get("/__test__/stats")
def stats():
    return {"started": len(upstream.started), "finished": len(upstream.finished),
            "cancelled": len(upstream.cancelled), "active": upstream.active,
            "auth_matches": all(upstream.auth_matches)}
