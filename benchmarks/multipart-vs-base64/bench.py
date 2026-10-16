# /// script
# requires-python = ">=3.14"
# dependencies = ["httpx2[http2]==2.13.1", "numpy==2.5.4", "pillow==12.3.0"]
# ///
"""Is it faster to send Jev images as multipart/form-data or as base64 in JSON?

Sends the same requests to the TypeSafe API both ways, with and without zstd
compression of the body, and records sizes, client encoding time, latency,
throughput, tokens, and answers. The images come from corpus.py: photos (JPEG
and PNG), document screenshots, and flat drawings at three sizes, each with a
red target whose shape and box are known.

    uv run benchmarks/multipart-vs-base64/bench.py offline                 # Free: sizes and encoding time
    uv run benchmarks/multipart-vs-base64/bench.py pilot --label pilot     # A few paid requests
    uv run benchmarks/multipart-vs-base64/bench.py battery --label fiber --profile full
    uv run benchmarks/multipart-vs-base64/summarize.py results/fiber

The battery's phases:

- sizes: every image kind and size, one image per request, four questions
  (Noul, Choice, Score, Box). Each image goes once through every transport,
  one request at a time, in rotating order, so each comparison is paired.
- placement: images in the state, in a question's instructions, as Choice
  criteria, as 8 frames, and as an album of 12, paired the same way.
- throughput: many requests at once per transport, in A B C D D C B A blocks.
- limits: a request of lossless photos that fits in 32 MiB as multipart but
  not as base64.

Results go to results/<label>/ (gitignored). Needs TYPESAFE_API_KEY for
everything but `offline`.
"""

import argparse
import asyncio
import base64
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from compression import zstd
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path

import httpx2

import corpus

HERE = Path(__file__).resolve().parent
URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/") + "/v1/systemone"
MODEL = "jev-1.14.0"
PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000
BODY_LIMIT = 32 * 1024 * 1024
TRANSPORTS = ("multipart", "multipart+zstd", "base64", "base64+zstd")
ALL_TRANSPORTS = (*TRANSPORTS, "multipart+gzip", "base64+gzip")
PROFILES = {
    # Throughput entries are (kind, size, concurrency, requests per block); each transport gets two blocks.
    # For a slow or metered link, such as a phone's hotspot: about 0.35 GB to upload.
    "quick": {"size_reps": 4, "placement_reps": 4, "limits": False,
              "throughput": [("photo", "M", 8, 24), ("photo", "M", 32, 24)]},
    # For a fast wired link: about 5.5 GB to upload.
    "full": {"size_reps": 16, "placement_reps": 12, "limits": True,
             "throughput": [("photo", "M", 8, 64), ("photo", "M", 32, 192), ("photo", "M", 128, 512),
                            ("photo_png", "M", 8, 48), ("photo_png", "M", 32, 128)]},
}
SHAPE_CRITERIA = {
    "circle": "A round disc.",
    "square": "A square with four equal sides.",
    "triangle": "A triangle.",
    "star": "A five-pointed star.",
}
COVERAGE_LEVELS = [
    "The red object covers under 2% of the image: a tiny mark.",
    "The red object covers 2% to 8% of the image: a small object.",
    "The red object covers 8% to 20% of the image: a medium object.",
    "The red object covers over 20% of the image: a large object.",
]
COVERAGE_BOUNDS = (0.02, 0.08, 0.20)


# --- Requests -----------------------------------------------------------------------------------


@dataclass
class Case:
    """One request: its JSON body with image references in place, the images, and the known answers."""

    id: str
    scenario: str
    kind: str
    size: str
    body: dict
    images: dict  # reference -> corpus.Picture
    truth: dict
    headers: dict = field(default_factory=dict)

    @property
    def image_bytes(self):
        return sum(len(p.data) for p in self.images.values())


def reference(*key):
    """A UUID reference, the same for the same key, so request bodies are identical between runs."""
    return str(uuid.UUID(bytes=hashlib.sha256(repr(key).encode()).digest()[:16]))


def coverage_level(target):
    return sum(target.area >= bound for bound in COVERAGE_BOUNDS)


def target_questions(path):
    return {
        "left_half": {"type": "noul", "instructions": f"Is the center of the red object in {path} in the left half of the image?"},
        "shape": {"type": "choice", "instructions": f"What shape is the red object in {path}?", "criteria": SHAPE_CRITERIA},
        "coverage": {"type": "score", "instructions": f"How much of the image in {path} does the red object cover?",
                     "criteria": COVERAGE_LEVELS},
        "where": {"type": "box", "instructions": f"the red object in {path}"},
    }


def target_truth(target):
    left, top, right, bottom = target.box
    return {"left_half": (left + right) / 2 < 0.5, "shape": target.shape, "coverage": coverage_level(target),
            "where": list(target.box)}


def case_mixed(kind, size, seed):
    pic = corpus.picture(kind, size, seed)
    ref = reference("mixed", kind, size, seed)
    body = {"model": MODEL, "state": {"image": ref}, "questions": target_questions("`image`")}
    return Case(f"mixed-{kind}-{size}-{seed}", "mixed", kind, size, body, {ref: pic}, target_truth(pic.target))


def case_instructions(seed):
    """A photo in the state, and an example picture inside one question's instructions."""
    pic = corpus.picture("photo", "M", seed)
    other = [s for s in corpus.SHAPES if s != pic.target.shape][seed % 3]
    shown = pic.target.shape if seed % 2 == 0 else other
    photo, example = reference("instructions", seed, "photo"), reference("instructions", seed, "example")
    body = {"model": MODEL, "state": {"photo": photo}, "questions": {
        "same_shape": {"type": "noul", "instructions": f"Is the red object in `photo` the same shape as the gray shape in this picture: {example}?"},
        "where": {"type": "box", "instructions": "the red object in `photo`"},
    }}
    truth = {"same_shape": shown == pic.target.shape, "where": list(pic.target.box)}
    return Case(f"instructions-{seed}", "instructions", "photo", "M", body,
                {photo: pic, example: corpus.example(shown)}, truth)


def case_criteria(seed):
    """A photo in the state, and a Choice whose four options are each defined by an example picture."""
    pic = corpus.picture("photo", "M", seed + 1000)
    photo = reference("criteria", seed, "photo")
    examples = {shape: reference("criteria", seed, shape) for shape in corpus.SHAPES}
    body = {"model": MODEL, "state": {"photo": photo}, "questions": {
        "shape": {"type": "choice", "instructions": "Which example picture has the same shape as the red object in `photo`?",
                  "criteria": examples},
        "left_half": target_questions("`photo`")["left_half"],
    }}
    images = {photo: pic, **{ref: corpus.example(shape) for shape, ref in examples.items()}}
    truth = {"shape": pic.target.shape, "left_half": target_truth(pic.target)["left_half"]}
    return Case(f"criteria-{seed}", "criteria", "photo", "M", body, images, truth)


def case_frames(seed, count=8):
    """A short history of camera frames in the state, oldest first."""
    moves = seed % 2 == 0
    pics = corpus.frames("M", seed, count, moves)
    refs = [reference("frames", seed, i) for i in range(count)]
    state = {"frames": [{"seconds_ago": count - 1 - i, "frame": ref} for i, ref in enumerate(refs)]}
    body = {"model": MODEL, "state": state, "questions": {
        "moved": {"type": "noul", "instructions": "Does the red object move to a different place during `frames`?"},
        "shape": target_questions(f"`frames[{count - 1}].frame`")["shape"],
        "where": target_questions(f"`frames[{count - 1}].frame`")["where"],
    }}
    truth = {"moved": moves, "shape": pics[-1].target.shape, "where": list(pics[-1].target.box)}
    return Case(f"frames-{seed}", "frames", "photo", "M", body, dict(zip(refs, pics)), truth)


def case_album(seed, count=12):
    """An album of small photos in the state; exactly one of them shows a red star."""
    star_at = seed % count
    others = [s for s in corpus.SHAPES if s != "star"]
    pics = [corpus.picture("photo", "S", (seed, i), shape="star" if i == star_at else others[(seed + i) % 3])
            for i in range(count)]
    refs = [reference("album", seed, i) for i in range(count)]
    options = {f"photo_{i + 1}": f"`album[{i}]`" for i in range(count)}
    body = {"model": MODEL, "state": {"album": refs}, "questions": {
        "star": {"type": "choice", "instructions": "Which photo in `album` shows a red five-pointed star?", "criteria": options},
        "all_red": {"type": "noul", "instructions": "Does every photo in `album` contain a red object?"},
    }}
    truth = {"star": f"photo_{star_at + 1}", "all_red": True}
    return Case(f"album-{seed}", "album", "photo", "S", body, dict(zip(refs, pics)), truth)


def case_limits(count):
    """Lossless photos whose multipart body is under 32 MiB but whose base64 body is over it.

    Seven 1568 × 1176 images are about 35,000 tokens, over the default 32,000 per question, so the request
    turns on long context to reach the body limit instead.
    """
    pics = [corpus.picture("photo_png", "L", ("limits", i)) for i in range(count)]
    refs = [reference("limits", i) for i in range(count)]
    body = {"model": MODEL, "state": {"photos": refs}, "questions": {
        "all_red": {"type": "noul", "instructions": "Does every photo in `photos` contain a red object?"},
    }}
    return Case(f"limits-{count}", "limits", "photo_png", "L", body, dict(zip(refs, pics)), {"all_red": True},
                {"typesafe-danger-zone-long-context": "true"})


def case_probe(kind, size, seed):
    """One image and one Noul, the smallest image request, to count tokens per image."""
    pic = corpus.picture(kind, size, seed)
    ref = reference("probe", kind, size, seed)
    body = {"model": MODEL, "state": {"image": ref},
            "questions": {"left_half": target_questions("`image`")["left_half"]}}
    return Case(f"probe-{kind}-{size}-{seed}", "probe", kind, size, body, {ref: pic},
                {"left_half": target_truth(pic.target)["left_half"]})


def text_case():
    """A request without images, to open connections."""
    body = {"model": MODEL, "state": "Warm-up.", "questions": {"ok": {"type": "noul", "instructions": "Is this a test?"}}}
    return Case("warmup", "warmup", "", "", body, {}, {})


def limits_count():
    """The number of lossless L photos whose multipart body is closest under 30 MiB."""
    count = 1
    while encode(case_limits(count + 1), "multipart").body_bytes < 30 * 1024 * 1024:
        count += 1
    return count


# --- Encoding -----------------------------------------------------------------------------------


@dataclass
class Encoded:
    headers: dict
    content: bytes
    body_bytes: int  # before compression
    encode_s: float


def encode_multipart(parts):
    """multipart/form-data, as the TypeSafe SDKs write it: one part per image, after the JSON request part."""
    boundary = uuid.uuid4().hex
    chunks = []
    for name, content_type, content, filename in parts:
        disposition = f'form-data; name="{name}"' + ("" if filename is None else f'; filename="{filename}"')
        chunks += (f"--{boundary}\r\nContent-Disposition: {disposition}\r\nContent-Type: {content_type}\r\n\r\n".encode(),
                   content, b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def dumps(value):
    return json.dumps(value, separators=(",", ":")).encode()


def encode(case, transport):
    """Build the request body for one transport, timing everything from the image bytes to the bytes sent."""
    method, _, compression = transport.partition("+")
    start = time.perf_counter()
    if not case.images:
        content_type, content = "application/json", dumps(case.body)
    elif method == "multipart":
        names = {ref: f"asset-{i}" for i, ref in enumerate(case.images)}
        assets = {ref: {"type": "image", "part": name} for ref, name in names.items()}
        parts = [("request", "application/json", dumps({**case.body, "assets": assets}), None)]
        parts += [(names[ref], pic.content_type, pic.data, names[ref]) for ref, pic in case.images.items()]
        content_type, content = encode_multipart(parts)
    elif method == "base64":
        assets = {ref: {"type": "image", "format": pic.format, "data_base64": base64.b64encode(pic.data).decode("ascii")}
                  for ref, pic in case.images.items()}
        content_type, content = "application/json", dumps({**case.body, "assets": assets})
    else:
        raise ValueError(f"Unknown transport {transport!r}")
    body_bytes = len(content)
    headers = {"content-type": content_type, **case.headers}
    if case.images or any(q["type"] == "box" for q in case.body["questions"].values()):
        headers["typesafe-danger-zone-images"] = "true"
    if compression == "zstd":
        content = zstd.compress(content)  # Level 3, as the SDKs use
        headers["content-encoding"] = "zstd"
    elif compression == "gzip":
        content = gzip.compress(content, compresslevel=6)
        headers["content-encoding"] = "gzip"
    elif compression:
        raise ValueError(f"Unknown compression {compression!r}")
    return Encoded(headers, content, body_bytes, time.perf_counter() - start)


# --- Sending ------------------------------------------------------------------------------------


def compact_answers(answers):
    out = {}
    for qid, a in (answers or {}).items():
        kind = a.get("type")
        if kind == "noul":
            out[qid] = round(a["noul"], 4)
        elif kind == "choice":
            out[qid] = {"choice": a["choice"], "p": round(a["probabilities"][a["choice"]], 4),
                        "confidence": round(a.get("confidence", 0), 4)}
        elif kind == "score":
            out[qid] = {"score": round(a["score"], 4), "confidence": round(a.get("confidence", 0), 4)}
        elif kind == "box":
            b = a["box"]
            out[qid] = {"box": [round(b["left"], 4), round(b["top"], 4), round(b["right"], 4), round(b["bottom"], 4)],
                        "confidence": round(a.get("confidence", 0), 4)}
        else:
            out[qid] = a
    return out


def retry_delay(response, attempt):
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = response.headers.get(name) if response is not None else None
        if value:
            try:
                return min(30.0, float(value) * scale)
            except ValueError:
                pass
    return min(30.0, 0.5 * 2 ** attempt)


class Runner:
    def __init__(self, out_dir, label, max_cost, http2=False, verbose=True):
        self.out_dir = out_dir
        self.label = label
        self.max_cost = max_cost
        self.http2 = http2
        self.verbose = verbose
        self.tokens = 0
        self.records = (out_dir / "requests.jsonl").open("a")
        self.headers_seen = None

    @property
    def cost(self):
        return self.tokens * PRICE_PER_INPUT_TOKEN

    def client(self, connections):
        limits = httpx2.Limits(max_connections=connections, max_keepalive_connections=connections, keepalive_expiry=60)
        timeout = httpx2.Timeout(300.0, connect=30.0)
        headers = {"authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"}
        return httpx2.AsyncClient(http2=self.http2, limits=limits, timeout=timeout, headers=headers)

    async def warm(self, client, connections):
        """Open `connections` connections with cheap text requests, so the measured requests reuse them."""
        records = await asyncio.gather(*(self.send(client, text_case(), "multipart", phase="warmup") for _ in range(connections)))
        return [r["connect_s"] for r in records if r.get("connect_s") is not None]

    async def send(self, client, case, transport, phase, encoded=None, extra=None, retries=4):
        if self.cost > self.max_cost:
            raise RuntimeError(f"Stopping: spent ${self.cost:.2f} of the ${self.max_cost:.2f} limit")
        if encoded is None:
            encoded = encode(case, transport)
        record = {"label": self.label, "phase": phase, "case": case.id, "scenario": case.scenario, "kind": case.kind,
                  "size": case.size, "transport": transport, "images": len(case.images), "image_bytes": case.image_bytes,
                  "body_bytes": encoded.body_bytes, "wire_bytes": len(encoded.content),
                  "encode_s": round(encoded.encode_s, 6), **(extra or {})}
        if "typesafe-danger-zone-long-context" in case.headers:
            record["long_context"] = True
        response = None
        for attempt in range(retries + 1):
            events = {}

            async def trace(name, info, events=events):
                events[name] = time.perf_counter()

            started = time.time()
            t0 = time.perf_counter()
            error = None
            try:
                response = await client.post(URL, content=encoded.content, headers=encoded.headers,
                                             extensions={"trace": trace})
            except httpx2.HTTPError as e:
                response, error = None, f"{type(e).__name__}: {e}"
            t1 = time.perf_counter()
            record.update(attempts=attempt + 1, started=round(started, 3), total_s=round(t1 - t0, 6),
                          **timings(events, t0, t1))
            status = response.status_code if response is not None else None
            if status in (429, 500, 502, 503, 504, 529) or error:
                record.setdefault("retried", []).append(status or error)
                if attempt < retries:
                    await asyncio.sleep(retry_delay(response, attempt))
                    continue
            break
        record["status"] = status
        if error:
            record["error"] = error
        elif response is not None:
            record["request_id"] = response.headers.get("x-typesafe-request-id")
            # Envoy's time from forwarding the request to TypeSafe's response headers. Cloudflare, in front of
            # Envoy, receives the whole upload first, so this is close to TypeSafe's own processing time.
            server_ms = response.headers.get("x-envoy-upstream-service-time")
            record["server_s"] = int(server_ms) / 1000 if server_ms and server_ms.isdigit() else None
            ray = response.headers.get("cf-ray", "")
            record["colo"] = ray.rpartition("-")[2] if "-" in ray else None
            if self.headers_seen is None:
                self.headers_seen = {k: v for k, v in response.headers.items() if k not in ("set-cookie", "date")}
            try:
                payload = response.json()
            except ValueError:
                payload = {"raw": response.text[:500]}
            if status == 200:
                usage = payload.get("usage", {})
                record["input_tokens"] = usage.get("input_tokens")
                record["model"] = payload.get("model")
                self.tokens += usage.get("input_tokens") or 0
                record["answers"] = compact_answers(payload.get("answers"))
                record["truth"] = case.truth
            else:
                record["error"] = json.dumps(payload)[:500]
        self.records.write(json.dumps(record) + "\n")
        self.records.flush()
        if self.verbose and phase != "warmup":
            print(f"  {phase:11} {case.id:28} {transport:15} {status} {record['total_s'] * 1000:8.0f} ms "
                  f"{record['wire_bytes'] / 1024:9,.0f} KiB {record.get('input_tokens') or '':>6} tok  ${self.cost:.3f}",
                  flush=True)
        return record


def timings(events, t0, t1):
    def at(name):
        for prefix in ("http11", "http2", "connection"):
            if f"{prefix}.{name}" in events:
                return events[f"{prefix}.{name}"]
        return None

    out = {}
    tcp0, tcp1 = at("connect_tcp.started"), at("connect_tcp.complete")
    tls0, tls1 = at("start_tls.started"), at("start_tls.complete")
    if tcp0 and tcp1:
        out["connect_s"] = round((tcp1 - tcp0) + ((tls1 - tls0) if tls0 and tls1 else 0), 6)
    head0, body1, resp1 = at("send_request_headers.started"), at("send_request_body.complete"), at("receive_response_headers.complete")
    if head0:
        out["queued_s"] = round(head0 - t0, 6)
    if head0 and body1:
        out["upload_s"] = round(body1 - head0, 6)  # until the last byte is handed to the socket, not acknowledged
    if body1 and resp1:
        out["wait_s"] = round(resp1 - body1, 6)
    if resp1:
        out["download_s"] = round(t1 - resp1, 6)
    return out


# --- Phases -------------------------------------------------------------------------------------


async def run_paired(runner, phase, cases, transports, retries=4):
    """Send each case through every transport, one request at a time, rotating which transport goes first."""
    async with runner.client(1) as client:
        await runner.warm(client, 1)
        for i, case in enumerate(cases):
            shift = i % len(transports)
            for position, transport in enumerate(transports[shift:] + transports[:shift]):
                await runner.send(client, case, transport, phase, extra={"position": position}, retries=retries)


async def run_throughput(runner, cases, concurrency, transports):
    """Send every case at once per transport, in A B C D D C B A blocks; returns one summary per block."""
    count, kind, size = len(cases), cases[0].kind, cases[0].size
    print(f"  encoding {count} requests x {len(transports)} transports", flush=True)
    encoded = {t: [encode(c, t) for c in cases] for t in transports}
    blocks = []
    async with runner.client(concurrency) as client:
        await runner.warm(client, concurrency)
        for block, transport in enumerate(transports + transports[::-1]):
            semaphore = asyncio.Semaphore(concurrency)

            async def one(i):
                async with semaphore:
                    return await runner.send(client, cases[i], transport, "throughput", encoded=encoded[transport][i],
                                             extra={"concurrency": concurrency, "block": block}, retries=6)

            start = time.perf_counter()
            records = await asyncio.gather(*(one(i) for i in range(count)))
            wall = time.perf_counter() - start
            ok = [r for r in records if r["status"] == 200]
            summary = {"kind": kind, "size": size, "concurrency": concurrency, "block": block, "transport": transport,
                       "requests": count, "ok": len(ok), "wall_s": round(wall, 3),
                       "images_per_s": round(sum(r["images"] for r in ok) / wall, 2),
                       "wire_mbit_per_s": round(sum(r["wire_bytes"] for r in records) * 8 / wall / 1e6, 2),
                       "retried": sum(len(r.get("retried", [])) for r in records),
                       "latency_p50_s": round(statistics.median(r["total_s"] for r in records), 4)}
            blocks.append(summary)
            print(f"  block {block} {transport:15} c={concurrency:<4} {summary['images_per_s']:8.1f} images/s "
                  f"{summary['wire_mbit_per_s']:7.1f} Mbit/s  p50 {summary['latency_p50_s']:.2f} s  "
                  f"retries {summary['retried']}", flush=True)
            with (runner.out_dir / "throughput.jsonl").open("a") as f:
                f.write(json.dumps({"label": runner.label, **summary}) + "\n")
    return blocks


# --- Environment --------------------------------------------------------------------------------


def network_interface():
    """The default route's interface and its hardware port name on macOS, such as Wi-Fi or an Ethernet adapter."""
    info = {}
    try:
        route = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
        match = re.search(r"interface:\s*(\S+)", route)
        if match:
            info["interface"] = match[1]
            ports = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5).stdout
            port = re.search(rf"Hardware Port: (.+)\nDevice: {re.escape(match[1])}\n", ports)
            if port:
                info["hardware_port"] = port[1]
            media = subprocess.run(["ifconfig", match[1]], capture_output=True, text=True, timeout=5).stdout
            found = re.search(r"media: (.+)", media)
            if found:
                info["media"] = found[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def network_quality():
    """macOS networkQuality: uplink and downlink capacity and responsiveness. It uploads and downloads for about 20 s."""
    if not shutil.which("networkQuality"):
        return None
    try:
        out = subprocess.run(["networkQuality", "-c"], capture_output=True, text=True, timeout=120).stdout
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    keep = ("dl_throughput", "ul_throughput", "responsiveness", "base_rtt", "interface_name")
    return {k: data[k] for k in keep if k in data}


def environment(args):
    return {
        "label": args.label, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": MODEL, "url": URL,
        "platform": platform.platform(), "machine": platform.machine(), "python": sys.version.split()[0],
        "packages": {p: version(p) for p in ("httpx2", "httpcore2", "h2", "numpy", "pillow")},
        "http2": args.http2, "network": network_interface(), "network_quality": network_quality() if args.network_quality else None,
    }


# --- Commands -----------------------------------------------------------------------------------


def offline(args):
    """Sizes and client encoding time for every transport, without sending anything."""
    rows = []
    groups = [(f"{kind} {size}", [case_mixed(kind, size, seed) for seed in range(3)]) for kind in corpus.KINDS for size in corpus.SIZES]
    groups += [("instructions", [case_instructions(s) for s in range(3)]), ("criteria", [case_criteria(s) for s in range(3)]),
               ("frames", [case_frames(s) for s in range(2)]), ("album", [case_album(s) for s in range(2)])]
    count = limits_count()
    groups.append((f"limits ({count} lossless L photos)", [case_limits(count)]))
    for name, cases in groups:
        for transport in ALL_TRANSPORTS:
            runs = [[encode(c, transport) for c in cases] for _ in range(args.repeat)]
            encode_ms = statistics.median(sum(e.encode_s for e in run) / len(run) for run in runs) * 1000
            last = runs[-1]
            rows.append({"group": name, "transport": transport, "images": len(cases[0].images),
                         "image_kib": statistics.mean(c.image_bytes for c in cases) / 1024,
                         "body_kib": statistics.mean(e.body_bytes for e in last) / 1024,
                         "wire_kib": statistics.mean(len(e.content) for e in last) / 1024,
                         "encode_ms": encode_ms,
                         "fits": all(e.body_bytes <= BODY_LIMIT and len(e.content) <= BODY_LIMIT for e in last)})
            r = rows[-1]
            print(f"{name:34} {transport:15} {r['image_kib']:10,.1f} KiB images  {r['body_kib']:10,.1f} KiB body  "
                  f"{r['wire_kib']:10,.1f} KiB sent  {r['encode_ms']:8.2f} ms{'' if r['fits'] else '  over 32 MiB'}", flush=True)
    out = HERE / "results" / "offline.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"environment": {"platform": platform.platform(), "machine": platform.machine(),
                                               "python": sys.version.split()[0]}, "rows": rows}, indent=1) + "\n")
    print(f"Wrote {out}")


def prepare_run(args):
    if "TYPESAFE_API_KEY" not in os.environ:
        sys.exit("Set TYPESAFE_API_KEY")
    out_dir = HERE / "results" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    env = environment(args)
    with (out_dir / "runs.jsonl").open("a") as f:
        f.write(json.dumps({**env, "command": args.command, "args": vars(args)}) + "\n")
    print(f"Writing to {out_dir}; network {env['network']}"
          + (f"; networkQuality {env['network_quality']}" if env["network_quality"] else ""), flush=True)
    return Runner(out_dir, args.label, args.max_cost, http2=args.http2)


async def pilot(args):
    """Tokens per image for each kind and size, the response headers, and whether repeating a request is faster."""
    runner = prepare_run(args)
    transports = args.transports
    async with runner.client(1) as client:
        connects = await runner.warm(client, 1)
        print(f"Connect + TLS: {connects}")
        for kind in corpus.KINDS:
            for size in corpus.SIZES:
                await runner.send(client, case_probe(kind, size, 0), transports[0], "probe")
        print("Response headers:", json.dumps(runner.headers_seen, indent=1))
        # The same request five times, then five different ones: a cache would make the repeats faster.
        same = case_mixed("photo", "M", "pilot0")
        for i in range(5):
            await runner.send(client, same, "multipart", "repeat", extra={"repeat": i})
        for i in range(5):
            await runner.send(client, case_mixed("photo", "M", f"pilot{i + 1}"), "multipart", "distinct", extra={"repeat": i})
    print(f"Spent ${runner.cost:.4f}")


def plan(args):
    """The battery's phases as (phase, cases, sends per case per transport, concurrency)."""
    profile = PROFILES[args.profile]
    phases = []
    if "sizes" in args.phases:
        reps = args.reps or profile["size_reps"]
        # Interleave kinds and sizes so drift in the network spreads over all of them.
        phases.append(("sizes", [case_mixed(kind, size, seed) for seed in range(reps) for size in corpus.SIZES
                                 for kind in corpus.KINDS], 1, 1))
    if "placement" in args.phases:
        reps = args.reps or profile["placement_reps"]
        phases.append(("placement", [c for seed in range(reps) for c in (case_instructions(seed), case_criteria(seed),
                                                                          case_frames(seed), case_album(seed))], 1, 1))
    if "throughput" in args.phases:
        for kind, size, concurrency, count in profile["throughput"]:
            phases.append(("throughput", [case_mixed(kind, size, f"t{seed}") for seed in range(count)], 2, concurrency))
    if "limits" in args.phases and (profile["limits"] or args.phases_given):
        phases.append(("limits", [case_limits(limits_count())], 1, 1))
    return phases


def estimate_tokens(case):
    """About 400 tokens of text per request, and one token per 390 pixels of each image, as measured on jev-1.14.0."""
    return 400 + sum(p.width * p.height / 390 for p in case.images.values())


async def battery(args):
    phases = plan(args)
    transports = list(args.transports)
    total_bytes = total_tokens = 0
    for phase, cases, sends, concurrency in phases:
        wire = sum(len(encode(c, t).content) for c in cases for t in transports) * sends
        tokens = sum(estimate_tokens(c) for c in cases) * len(transports) * sends
        total_bytes += wire
        total_tokens += tokens
        print(f"{phase:11} c={concurrency:<4} {len(cases) * len(transports) * sends:5} requests  "
              f"{wire / 1e6:9,.1f} MB to upload  about ${tokens * PRICE_PER_INPUT_TOKEN:.2f}")
    print(f"Total: {total_bytes / 1e9:.2f} GB to upload, about ${total_tokens * PRICE_PER_INPUT_TOKEN:.2f}", flush=True)
    if args.dry_run:
        return
    runner = prepare_run(args)
    try:
        for phase, cases, sends, concurrency in phases:
            print(f"Phase {phase}" + (f" at concurrency {concurrency}" if phase == "throughput" else ""), flush=True)
            if phase == "throughput":
                await run_throughput(runner, cases, concurrency, transports)
            elif phase == "limits":
                # An oversized body can end in a reset connection; retrying would only upload it again.
                await run_paired(runner, phase, cases, transports, retries=0)
            else:
                await run_paired(runner, phase, cases, transports)
    finally:
        print(f"Spent ${runner.cost:.4f} ({runner.tokens:,} input tokens)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("offline", "pilot", "battery"))
    parser.add_argument("--label", default=time.strftime("run-%Y%m%d-%H%M%S"), help="Results go to results/<label>/")
    parser.add_argument("--profile", choices=PROFILES, default="full")
    parser.add_argument("--phases", help="Comma-separated: sizes,placement,throughput,limits (default: all in the profile)")
    parser.add_argument("--reps", type=int, help="Override the profile's repetitions for sizes and placement")
    parser.add_argument("--transports", default=",".join(TRANSPORTS), help=f"Comma-separated, from {', '.join(ALL_TRANSPORTS)}")
    parser.add_argument("--http2", action="store_true", help="Use HTTP/2 instead of HTTP/1.1, the SDK default")
    parser.add_argument("--network-quality", action="store_true", help="Record macOS networkQuality first (about 20 s)")
    parser.add_argument("--dry-run", action="store_true", help="battery: print the plan, upload volume, and cost, then stop")
    parser.add_argument("--max-cost", type=float, default=20.0, help="Stop once this many dollars of input tokens are spent")
    parser.add_argument("--repeat", type=int, default=5, help="offline: timing repetitions")
    args = parser.parse_args()
    args.transports = args.transports.split(",")
    if unknown := set(args.transports) - set(ALL_TRANSPORTS):
        parser.error(f"Unknown transports: {', '.join(sorted(unknown))}")
    args.phases_given = args.phases is not None
    args.phases = (args.phases or "sizes,placement,throughput,limits").split(",")
    if args.command == "offline":
        offline(args)
    elif args.command == "pilot":
        asyncio.run(pilot(args))
    else:
        asyncio.run(battery(args))


if __name__ == "__main__":
    main()
