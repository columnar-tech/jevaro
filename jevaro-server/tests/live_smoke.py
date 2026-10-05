"""Optional live check: three states through each SDK, as JSON and as Arrow IPC (12 paid evaluations)."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pyarrow as pa
import uvicorn

from jevaro import TypeSafeClient
from jevaro_server.app import create_app

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
            print(f"Twelve live evaluations passed in {time.monotonic() - started:.2f}s")
        except subprocess.CalledProcessError as error:
            raise RuntimeError(error.stderr) from error
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
