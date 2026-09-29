"""Check release metadata, required files, and consistency with this checkout."""

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def archive_files(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    with tarfile.open(path, "r:gz") as archive:
        return {member.name: archive.extractfile(member).read()
                for member in archive.getmembers() if member.isfile()}


def check(dist):
    projects = {directory: tomllib.loads((ROOT / directory / "pyproject.toml").read_text())["project"]
                for directory in ("jevaro-server", "jevaro-python")}
    npm = json.loads((ROOT / "jevaro-javascript/package.json").read_text())
    lock = json.loads((ROOT / "jevaro-javascript/package-lock.json").read_text())
    python_versions = {project["version"] for project in projects.values()}
    require(len(python_versions) == 1, "Python package versions do not match")
    python_version = python_versions.pop()
    javascript_version = npm["version"]
    require(javascript_version == lock["version"] == lock["packages"][""]["version"],
            "JavaScript package and lockfile versions do not match")
    expected = {f"jevaro-{javascript_version}.tgz"}
    for project in projects.values():
        stem = f"{project['name'].replace('-', '_')}-{python_version}"
        expected.update((f"{stem}.tar.gz", f"{stem}-py3-none-any.whl"))
    actual = {p.name for p in dist.iterdir() if p.is_file()}
    require(actual == expected, f"Expected five release files; missing={expected - actual}, extra={actual - expected}")

    for directory, project in projects.items():
        package = project["name"].replace("-", "_")
        stem = f"{package}-{python_version}"
        wheel = archive_files(dist / f"{stem}-py3-none-any.whl")
        sdist = archive_files(dist / f"{stem}.tar.gz")
        info = f"{stem}.dist-info"
        for data in (wheel[f"{info}/METADATA"], sdist[f"{stem}/PKG-INFO"]):
            metadata = BytesParser().parsebytes(data)
            for key, value in (("Name", project["name"]), ("Version", python_version),
                               ("Summary", project["description"]),
                               ("License-Expression", "Apache-2.0"),
                               ("Requires-Python", project["requires-python"]),
                               ("Author", "Columnar Technologies Inc."),
                               ("Description-Content-Type", "text/markdown")):
                require(metadata[key] == value, f"{stem}: wrong {key}")
            require(set(metadata.get_all("License-File", [])) == {"LICENSE", "NOTICE"},
                    f"{stem}: missing license declarations")
            require(metadata.get_payload().strip() == (ROOT / directory / "README.md").read_text().strip(),
                    f"{stem}: stale README")
        for filename in ("LICENSE", "NOTICE"):
            data = (ROOT / filename).read_bytes()
            require(wheel[f"{info}/licenses/{filename}"] == data, f"{stem}: wrong wheel {filename}")
            require(sdist[f"{stem}/{filename}"] == data, f"{stem}: wrong sdist {filename}")
        for source in (ROOT / directory / "src" / package).iterdir():
            if source.is_file() and (source.suffix == ".py" or source.name == "py.typed"):
                require(wheel[f"{package}/{source.name}"] == source.read_bytes(), f"{stem}: stale wheel {source.name}")
                require(sdist[f"{stem}/src/{package}/{source.name}"] == source.read_bytes(), f"{stem}: stale sdist {source.name}")
        require(b"Tag: py3-none-any" in wheel[f"{info}/WHEEL"], f"{stem}: unexpected wheel platform")
        if package == "jevaro_server":
            require(b"jevaro-server = jevaro_server.__main__:main" in wheel[f"{info}/entry_points.txt"],
                    "Missing server command")
            require(f"{stem}/tests/fake_upstream.py" in sdist, "Missing server test fixture")
        else:
            require(f"{package}/py.typed" in wheel, "Missing Python typing marker")
            require(sdist[f"{stem}/example.py"] == (ROOT / directory / "example.py").read_bytes(),
                    "Missing or stale Python example")

    javascript = archive_files(dist / f"jevaro-{javascript_version}.tgz")
    files = {"package.json", *npm["files"]}
    require(set(javascript) == {f"package/{name}" for name in files}, "Unexpected npm tarball contents")
    for name in files:
        require(javascript[f"package/{name}"] == (ROOT / "jevaro-javascript" / name).read_bytes(),
                f"Stale npm file: {name}")
    for name in ("LICENSE", "NOTICE"):
        require(javascript[f"package/{name}"] == (ROOT / name).read_bytes(), f"Wrong npm {name}")
    print(f"Validated Python {python_version} and JavaScript {javascript_version} distributions")
    for name in sorted(expected):
        print(f"{hashlib.sha256((dist / name).read_bytes()).hexdigest()}  {name}")
    return python_version, javascript_version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, nargs="?", default=ROOT / "dist")
    check(parser.parse_args().dist.resolve())
