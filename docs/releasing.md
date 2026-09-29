# Build and test a release

All three packages use the same version initially. The first release is `0.1.0`.

## Build

From the repository root, in an environment with Python 3.11+ and Node.js 20.3+:

```sh
python -m pip install -r requirements-build.txt
python -m build --outdir dist jevaro-server
python -m build --outdir dist jevaro-python
npm pack ./jevaro-javascript --pack-destination dist
python -m twine check --strict dist/*.whl dist/*.tar.gz
python scripts/check_dist.py dist
```

Use an empty `dist` directory when changing versions. The check rejects extra
archives so an older version cannot slip into a release.

This produces two wheels, two Python source archives, and one npm tarball.
Each Python wheel is built from its source archive. The checks verify versions,
licenses, README metadata, code, typing files, examples, and the server command.

## Test the files users will install

```sh
python scripts/test_dist.py dist
```

This creates a temporary Python environment and Node project, installs the
archives, and runs the Python, JavaScript, TypeScript, and HTTP tests. It also
saves and decodes an Arrow file. Only tests and examples are copied from the
checkout; package code comes from the built archives.

Dependencies are downloaded as needed. API responses are simulated locally;
no TypeSafe key or paid calls are needed. Temporary files are removed afterward.

## CI

[GitHub Actions](../.github/workflows/ci.yml) builds the archives once, then
tests those same files on Python 3.11 and 3.14 with Node 20.3.0 and 24. Checks
run on pushes, pull requests, and manual runs. Download the `distributions`
artifact from a successful run to get the five tested release files.

The workflow builds and tests only. Registry publishing is a later step in the
[release plan](release-plan.md).

## Change the version

1. Update `version` in both Python `pyproject.toml` files.
2. From `jevaro-javascript`, run `npm version X.Y.Z --no-git-tag-version`.
   This updates `package.json` and `package-lock.json` together.
3. Update the [changelog](../CHANGELOG.md), then build and test again.
4. After the release checks pass, use one source tag, `vX.Y.Z`, for all packages.

For the first release, leave `0.1.0` marked unreleased until the packages are
published, then record the release date.
