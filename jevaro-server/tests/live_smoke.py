"""Optional live check: three states through each SDK (six paid evaluations)."""

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

from jevaro_server.app import create_app


def main():
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise SystemExit("Set TYPESAFE_API_KEY before running the live smoke test")
    root = Path(__file__).resolve().parents[2]
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
                    [sys.executable, str(root / "read_results.py"), str(output)],
                    capture_output=True, text=True, timeout=10, check=True,
                )
                assert len(decoded.stdout.splitlines()) == 3
                assert len(python.stdout.splitlines()) == 3
                print("Python: 3 states streamed, saved as IPC, and decoded from schema metadata")
            javascript = subprocess.run(
                ["node", str(root / "jevaro-javascript/example.mjs")],
                env=env, capture_output=True, text=True, timeout=90, check=True,
            )
            assert len(javascript.stdout.splitlines()) == 3
            print("JavaScript: 3 states streamed and decoded using the schema's labels")
            print(f"Six live evaluations passed in {time.monotonic() - started:.2f}s")
        except subprocess.CalledProcessError as error:
            raise RuntimeError(error.stderr) from error
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
