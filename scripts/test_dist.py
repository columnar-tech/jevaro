"""Install release packages in a fresh environment and run offline API tests."""

import argparse
from email.parser import BytesParser
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
    parser.add_argument("--python-index-url", help="Install Python packages from this index; dependencies use PyPI")
    parser.add_argument("--npm-registry-url", help="Install the JavaScript package from this npm registry")
    args = parser.parse_args()
    dist = args.dist.resolve()
    python_version, javascript_version = check(dist)
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
        wheels = sorted(dist.glob("*.whl"))
        if args.python_index_url:
            dependencies = set()
            packages = []
            for wheel in wheels:
                metadata = next(data for name, data in archive_files(wheel).items()
                                if name.endswith(".dist-info/METADATA"))
                metadata = BytesParser().parsebytes(metadata)
                dependencies.update(metadata.get_all("Requires-Dist", []))
                packages.append(f"{metadata['Name']}=={metadata['Version']}")
            run(python, "-m", "pip", "--isolated", "install", "--index-url", "https://pypi.org/simple/",
                *sorted(dependencies))
            run(python, "-m", "pip", "--isolated", "install", "--index-url", args.python_index_url,
                "--no-deps", "--no-cache-dir", *packages)
        else:
            run(python, "-m", "pip", "install", *wheels)
        run(python, "-m", "pip", "check")
        run(python, "-I", "-c", "from pathlib import Path; import sys, jevaro, jevaro_server; "
            "assert all(Path(m.__file__).is_relative_to(sys.prefix) for m in (jevaro, jevaro_server))")
        run(binaries / ("jevaro-server.exe" if os.name == "nt" else "jevaro-server"), "--help")

        for directory in ("jevaro-python", "jevaro-server"):
            shutil.copytree(ROOT / directory / "tests", work / directory / "tests",
                            ignore=shutil.ignore_patterns("__pycache__"))
        python_source = archive_files(dist / f"jevaro-{python_version}.tar.gz")
        (work / "jevaro-python/example.py").write_bytes(python_source[f"jevaro-{python_version}/example.py"])
        (work / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/read_results.py", work / "scripts/read_results.py")

        javascript = work / "jevaro-javascript"
        shutil.copytree(ROOT / "jevaro-javascript/test", javascript / "test")
        lock = json.loads((ROOT / "jevaro-javascript/package-lock.json").read_text())["packages"]
        consumer = {
            "private": True,
            "dependencies": {
                "jevaro": javascript_version if args.npm_registry_url
                else (dist / f"jevaro-{javascript_version}.tgz").as_uri(),
            },
            "devDependencies": {name: lock[f"node_modules/{name}"]["version"]
                                for name in ("typescript", "@types/node")},
        }
        (javascript / "package.json").write_text(json.dumps(consumer))
        npm_install = ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]
        if args.npm_registry_url:
            npm_install.extend(("--registry", args.npm_registry_url, "--cache", work / "npm-cache"))
        run(*npm_install, cwd=javascript)
        shutil.copyfile(javascript / "node_modules/jevaro/example.mjs", javascript / "example.mjs")
        run("node", "--test", "test/client.test.cjs", cwd=javascript)
        run("node", "node_modules/typescript/bin/tsc", "--noEmit", "-p", "test/tsconfig.json", cwd=javascript)
        run(python, "-m", "unittest", "discover", "-s", "jevaro-python/tests", "-v")
        # This also runs the JavaScript HTTP tests and the saved-file examples.
        run(python, "-m", "unittest", "discover", "-s", "jevaro-server/tests", "-v")
        print(f"Release files passed on Python {sys.version.split()[0]}", flush=True)


if __name__ == "__main__":
    main()
