"""Offline test fixture: real TypeSafe SDK over a mocked upstream transport."""

import asyncio
import json
import threading
from collections import Counter

import httpx2
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from jevaro_server.app import create_app


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
        self.gate = threading.Event()
        self.transport = httpx2.MockTransport(self.handle)

    def client(self, **kwargs):
        self.http_clients.append(kwargs["http_client"])
        return AsyncTypeSafeClient(
            **kwargs, retry=RetryPolicy(backoff_initial=0.001, backoff_jitter=0),
        )

    async def handle(self, request):
        payload = json.loads(request.content)
        state = payload["state"]
        config = state if isinstance(state, dict) else {}
        number = config.get("id", 0)
        self.started.append(state)
        self.auth_matches.append(request.headers.get("Authorization") == "Bearer jevaro-test-key")
        self.attempts[number] += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if config.get("gate"):
                while not self.gate.is_set():
                    await asyncio.sleep(0.005)
            await asyncio.sleep(config.get("delay", 0))
            statuses = config.get("statuses", [])
            attempt = self.attempts[number] - 1
            if attempt < len(statuses):
                return httpx2.Response(statuses[attempt], json={"error": "test failure"},
                                       headers={"retry-after-ms": "1"})
            answers = {}
            for name, question in payload["questions"].items():
                kind = question["type"]
                if kind == "noul":
                    answers[name] = {"type": kind, "noul": (number % 1000) / 1000}
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
            self.finished.append(number)
            return httpx2.Response(200, json={
                "answers": answers, "model": payload["model"],
                "usage": {"input_tokens": 42, "output_tokens": 4},
            })
        except asyncio.CancelledError:
            self.cancelled.append(number)
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
