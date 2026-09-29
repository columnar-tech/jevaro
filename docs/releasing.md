# Build and test a release

The Python packages share a version. The JavaScript SDK can be released
separately. All three started at `0.1.0`.

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

## npm release

For the first release, sign in with the npm account that will maintain the
package:

```sh
npm login --registry=https://registry.npmjs.org
npm whoami --registry=https://registry.npmjs.org
```

Direct publication requires two-factor authentication on the account. See
[npm's publishing instructions](https://docs.npmjs.com/creating-and-publishing-unscoped-public-packages/).

Download `distributions` from a successful CI run to `dist`, then publish the
tested JavaScript archive:

```sh
npm publish ./dist/jevaro-0.2.0.tgz --access public --registry=https://registry.npmjs.org
```

Verify fresh installs from both production registries:

```sh
python scripts/test_dist.py dist \
  --python-index-url https://pypi.org/simple/ \
  --npm-registry-url https://registry.npmjs.org/
```

The npm check uses a new cache and installs `jevaro` by version from the
registry. Keep the original archive and compare its integrity hash with
`npm view jevaro@0.2.0 dist.integrity`.

For later releases, the manual [npm workflow](../.github/workflows/npm.yml)
builds and tests the packages, then uploads the same JavaScript archive.
Configure [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/)
in the `jevaro` package settings with these values:

| Field | Value |
| --- | --- |
| Publisher | GitHub Actions |
| Organization or user | `columnar-tech` |
| Repository | `jevaro` |
| Workflow filename | `npm.yml` |
| Environment | `npm` |
| Allowed actions | Enable `npm publish` |

After changing the version and committing it to `main`, run:

```sh
gh workflow run npm.yml --ref main
```

The workflow uses OIDC; it needs no npm token. A published version cannot be
replaced. If an upload fails, check registry state before retrying.

## Change the version

For a Python release, update `version` in both Python `pyproject.toml` files.
Update the server's FastAPI version in `jevaro-server/src/jevaro_server/app.py`.
For a JavaScript release, run `npm version X.Y.Z --no-git-tag-version` from
`jevaro-javascript`; this updates `package.json` and `package-lock.json` together.
Update the browser example's CDN version in its HTML file and README.

Update the [changelog](../CHANGELOG.md), then build and test all packages
together. Run the publishing workflow for each registry whose version changed.
Record the published versions and release date, then tag the source as `vX.Y.Z`.
