"""Optional live check: three states through each SDK, as JSON and as Arrow IPC, and packed,
plus a Box question about an image through each SDK (19 paid evaluations)."""

import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

import pyarrow as pa
import uvicorn

from jevaro import Box, Image, Noul, TypeSafeClient
from jevaro_server.app import create_app

IMAGES = {"TypeSafe-Danger-Zone-Images": "true"}
# Where draw_png puts the red rectangle, as fractions of the image's width and height.
RED = {"top": 0.5, "bottom": 0.75, "left": 0.25, "right": 0.5}
IMAGE_QUESTIONS = {
    "red": Box(instructions="the red rectangle in `photo`"),
    "seen": Noul(instructions="Is there a red rectangle in `photo`?"),
}

# Sends the example states as an Arrow Table from JavaScript; argv[1] is JSON [states, questions].
JAVASCRIPT_ARROW = """
import { tableFromArrays } from "apache-arrow";
import { TypeSafeClient } from "jevaro";

const [states, questions] = JSON.parse(process.argv[1]);
const reader = await new TypeSafeClient().systemOne({
  states: tableFromArrays({ message: states }), stateColumn: "message", questions,
});
const field = reader.schema.fields.find(f => f.name === "department");
const { labels } = JSON.parse(field.metadata.get("ARROW:extension:metadata"));
for await (const batch of reader) {
  for (const row of batch) console.log(labels[row.department.choice]);
}
"""


def draw_png(width=512, height=384):
    """A white PNG with a red rectangle at RED."""
    rows = []
    for y in range(height):
        inside = RED["top"] * height <= y < RED["bottom"] * height
        rows.append(b"\x00" + b"".join(
            b"\xdc\x1e\x1e" if inside and RED["left"] * width <= x < RED["right"] * width else b"\xff\xff\xff"
            for x in range(width)))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def check_box(box, label):
    """The red rectangle's box, within 0.05 on each edge."""
    misses = {edge: round(box[edge] - RED[edge], 3) for edge in RED if abs(box[edge] - RED[edge]) > 0.05}
    assert not misses, f"{label}: {box} is off by {misses}"


# Sends the PNG at argv[1] as an image() state and an Arrow binary column; prints each box as JSON.
JAVASCRIPT_IMAGES = """
import { readFileSync } from "node:fs";
import { Binary, Table, vectorFromArray } from "apache-arrow";
import { TypeSafeClient, box, image, noul } from "jevaro";

const png = readFileSync(process.argv[1]);
const client = new TypeSafeClient();
const questions = { red: box("the red rectangle in `photo`"), seen: noul("Is there a red rectangle in `photo`?") };
const headers = { "TypeSafe-Danger-Zone-Images": "true" };
for (const states of [[{ photo: image(png) }], new Table({ photo: vectorFromArray([png], new Binary()) })]) {
  const reader = await client.systemOne({ states, questions, model: "jev-preview" }, { headers });
  for await (const batch of reader) for (const row of batch) console.log(JSON.stringify(row.red.box.toJSON()));
}
"""


def departments(table):
    labels = json.loads(table.schema.field("department").metadata[b"ARROW:extension:metadata"])["labels"]
    return [labels[row["choice"]] for row in table["department"].to_pylist()]


def main():
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise SystemExit("Set TYPESAFE_API_KEY before running the live smoke test")
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "jevaro-python"))
    from example import QUESTIONS, STATES

    raw_questions = {name: question.model_dump(mode="json") for name, question in QUESTIONS.items()}
    started = time.monotonic()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(create_app(), log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("Server did not start")
                time.sleep(0.01)
            env = {**os.environ, "TYPESAFE_BASE_URL": base_url}
            with tempfile.TemporaryDirectory(prefix="jevaro-smoke-") as directory:
                output = Path(directory) / "results.arrows"
                python = subprocess.run(
                    [sys.executable, str(root / "jevaro-python/example.py"), "--output", str(output)],
                    env=env, capture_output=True, text=True, timeout=90, check=True,
                )
                with pa.ipc.open_stream(output) as reader:
                    assert reader.read_all().num_rows == 3
                decoded = subprocess.run(
                    [sys.executable, str(root / "scripts/read_results.py"), str(output)],
                    capture_output=True, text=True, timeout=10, check=True,
                )
                assert len(decoded.stdout.splitlines()) == 3
                assert len(python.stdout.splitlines()) == 3
                print("Python: 3 states streamed, saved as IPC, and decoded from schema metadata")
            expected = [json.loads(line)["department"] for line in python.stdout.splitlines()]
            with TypeSafeClient(base_url=base_url) as client:
                with client.system_one(states=pa.table({"message": STATES}), state_column="message",
                                       questions=QUESTIONS) as reader:
                    assert departments(reader.read_all()) == expected
            print("Python: the same 3 states sent as Arrow IPC got the same departments")
            with TypeSafeClient(base_url=base_url) as client:
                with client.system_one(states=STATES, questions=QUESTIONS, rows_per_call=3) as reader:
                    packed = departments(reader.read_all())
            assert len(packed) == 3
            same = sum(a == b for a, b in zip(packed, expected))
            print(f"Python: the same 3 states packed into one call; {same} of 3 departments match")
            javascript = subprocess.run(
                ["node", str(root / "jevaro-javascript/example.mjs")],
                env=env, capture_output=True, text=True, timeout=90, check=True,
            )
            assert len(javascript.stdout.splitlines()) == 3
            print("JavaScript: 3 states streamed and decoded using the schema's labels")
            arrow = subprocess.run(
                ["node", "--input-type=module", "-e", JAVASCRIPT_ARROW, json.dumps([STATES, raw_questions])],
                cwd=root / "jevaro-javascript", env=env, capture_output=True, text=True, timeout=90, check=True,
            )
            assert arrow.stdout.splitlines() == expected
            print("JavaScript: the same 3 states sent as an Arrow Table got the same departments")
            png = draw_png()
            with TypeSafeClient(base_url=base_url, model="jev-preview") as client:
                with client.system_one(states=[{"photo": Image(png)}], questions=IMAGE_QUESTIONS,
                                       extra_headers=IMAGES) as reader:
                    (row,) = reader.read_all().to_pylist()
                check_box(row["red"]["box"], "Python image")
                assert row["seen"] > 0.5, row
                with client.system_one(states=pa.table({"photo": [png]}), questions=IMAGE_QUESTIONS,
                                       box_format="geoarrow", extra_headers=IMAGES) as reader:
                    (row,) = reader.read_all().to_pylist()
                edges = row["red"]["box"]
                check_box({"top": edges["ymin"], "bottom": edges["ymax"], "left": edges["xmin"],
                           "right": edges["xmax"]}, "Python Arrow binary")
            print("Python: Box found the red rectangle in an Image and in an Arrow binary column")
            with tempfile.TemporaryDirectory(prefix="jevaro-smoke-") as directory:
                path = Path(directory) / "red.png"
                path.write_bytes(png)
                boxes = subprocess.run(
                    ["node", "--input-type=module", "-e", JAVASCRIPT_IMAGES, str(path)],
                    cwd=root / "jevaro-javascript", env=env, capture_output=True, text=True, timeout=90, check=True,
                )
            boxes = [json.loads(line) for line in boxes.stdout.splitlines()]
            assert len(boxes) == 2, boxes
            for box in boxes:
                check_box(box, "JavaScript")
            print("JavaScript: Box found the red rectangle in an image() and in an Arrow binary column")
            print(f"Nineteen live evaluations passed in {time.monotonic() - started:.2f}s")
        except subprocess.CalledProcessError as error:
            raise RuntimeError(error.stderr) from error
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
