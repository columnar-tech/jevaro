"""Install release files in a fresh environment and run offline API tests."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from check_dist import ROOT, archive_files, check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, nargs="?", default=ROOT / "dist")
    dist = parser.parse_args().dist.resolve()
    version = check(dist)
    env = {key: value for key, value in os.environ.items()
           if key != "PYTHONPATH" and not key.startswith(("TYPESAFE_", "JEVARO_"))}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    with tempfile.TemporaryDirectory(prefix="jevaro-dist-") as temporary:
        work = Path(temporary)

        def run(*args, cwd=work):
            print("+", " ".join(map(str, args)), flush=True)
            subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True, timeout=300)

        run(sys.executable, "-m", "venv", work / ".venv")
        binaries = work / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        python = binaries / ("python.exe" if os.name == "nt" else "python")
        run(python, "-m", "pip", "install", *sorted(dist.glob("*.whl")))
        run(python, "-m", "pip", "check")
        run(python, "-I", "-c", "from pathlib import Path; import sys, jevaro, jevaro_server; "
            "assert all(Path(m.__file__).is_relative_to(sys.prefix) for m in (jevaro, jevaro_server))")
        run(binaries / ("jevaro-server.exe" if os.name == "nt" else "jevaro-server"), "--help")

        for directory in ("jevaro-python", "jevaro-server"):
            shutil.copytree(ROOT / directory / "tests", work / directory / "tests",
                            ignore=shutil.ignore_patterns("__pycache__"))
        python_source = archive_files(dist / f"jevaro-{version}.tar.gz")
        (work / "jevaro-python/example.py").write_bytes(python_source[f"jevaro-{version}/example.py"])
        (work / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/read_results.py", work / "scripts/read_results.py")

        javascript = work / "jevaro-javascript"
        shutil.copytree(ROOT / "jevaro-javascript/test", javascript / "test")
        lock = json.loads((ROOT / "jevaro-javascript/package-lock.json").read_text())["packages"]
        consumer = {
            "private": True,
            "dependencies": {"jevaro": (dist / f"jevaro-{version}.tgz").as_uri()},
            "devDependencies": {name: lock[f"node_modules/{name}"]["version"]
                                for name in ("typescript", "@types/node")},
        }
        (javascript / "package.json").write_text(json.dumps(consumer))
        run("npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", cwd=javascript)
        shutil.copyfile(javascript / "node_modules/jevaro/example.mjs", javascript / "example.mjs")
        run("node", "--test", "test/client.test.cjs", cwd=javascript)
        run("node", "node_modules/typescript/bin/tsc", "--noEmit", "-p", "test/tsconfig.json", cwd=javascript)
        run(python, "-m", "unittest", "discover", "-s", "jevaro-python/tests", "-v")
        # This also runs the JavaScript HTTP tests and the saved-file examples.
        run(python, "-m", "unittest", "discover", "-s", "jevaro-server/tests", "-v")
        print(f"Release files passed on Python {sys.version.split()[0]}", flush=True)


if __name__ == "__main__":
    main()
