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

CI also runs as part of the manual TestPyPI and PyPI workflows below.

## TestPyPI rehearsal

The [TestPyPI workflow](../.github/workflows/testpypi.yml) builds and tests the
packages using CI, then uploads the same Python wheels and source archives.
It runs manually from `main`. Each Python package has its own publishing job
and GitHub environment.

In [TestPyPI's publishing settings](https://test.pypi.org/manage/account/publishing/),
add two pending publishers under GitHub:

| Field | Python SDK | Server |
| --- | --- | --- |
| PyPI project name | `jevaro` | `jevaro-server` |
| Owner | `columnar-tech` | `columnar-tech` |
| Repository name | `jevaro` | `jevaro` |
| Workflow name | `testpypi.yml` | `testpypi.yml` |
| Environment name | `testpypi` | `testpypi-server` |

The environments must differ: PyPI rejects pending publishers with the same
owner, repository, workflow, and environment for different project names.
See [PyPI's tracking issue](https://github.com/pypi/warehouse/issues/16920).

This uses [trusted publishing](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/);
no API token is needed. After both publishers are registered, run:

```sh
gh workflow run testpypi.yml --ref main
```

Download the run's `distributions` artifact to retain the tested files. Verify
fresh installs from TestPyPI:

```sh
python scripts/test_dist.py dist --python-index-url https://test.pypi.org/simple/
```

This installs `jevaro` and `jevaro-server` from TestPyPI with `--no-deps`,
installs dependencies from ordinary PyPI, and runs the same offline tests.
The JavaScript package is still installed from its local tarball.

If an upload fails, check which files reached TestPyPI before retrying. Preserve
the original artifacts; an uploaded filename cannot be replaced. This workflow
does not skip existing files.

## PyPI release

The [PyPI workflow](../.github/workflows/pypi.yml) uses the same build and tests
as TestPyPI. It runs manually from `main` and uploads to production PyPI.

In [PyPI's publishing settings](https://pypi.org/manage/account/publishing/),
add two pending publishers under GitHub:

| Field | Python SDK | Server |
| --- | --- | --- |
| PyPI project name | `jevaro` | `jevaro-server` |
| Owner | `columnar-tech` | `columnar-tech` |
| Repository name | `jevaro` | `jevaro` |
| Workflow name | `pypi.yml` | `pypi.yml` |
| Environment name | `pypi` | `pypi-server` |

After both publishers are registered, run:

```sh
gh workflow run pypi.yml --ref main
```

Download the run's `distributions` artifact and verify fresh registry installs:

```sh
python scripts/test_dist.py dist --python-index-url https://pypi.org/simple/
```

If an upload fails, inspect which files reached PyPI before retrying. Retain
the original artifacts; uploaded filenames cannot be replaced. Existing files
cause this workflow to fail rather than being skipped.

## Change the version

1. Update `version` in both Python `pyproject.toml` files.
2. From `jevaro-javascript`, run `npm version X.Y.Z --no-git-tag-version`.
   This updates `package.json` and `package-lock.json` together.
3. Update the [changelog](../CHANGELOG.md), then build and test again.
4. After the release checks pass, use one source tag, `vX.Y.Z`, for all packages.

For the first release, leave `0.1.0` marked unreleased until the packages are
published, then record the release date.
