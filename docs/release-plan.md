# Jevaro release plan

Source is public at [columnar-tech/jevaro](https://github.com/columnar-tech/jevaro).
The Python packages are published on PyPI and the JavaScript SDK is published
on npm. Both registries use trusted publishing from GitHub Actions.

## Decisions

| Item | Decision |
| --- | --- |
| Source repository | One repository: `columnar-tech/jevaro` |
| License | Apache-2.0 |
| Copyright holder | Columnar Technologies Inc. |
| Server distribution | PyPI: `jevaro-server`; command: `jevaro-server` |
| Python SDK distribution | PyPI: `jevaro`; import: `jevaro` |
| JavaScript SDK distribution | npm: `jevaro`; import/require: `jevaro` |
| First release | `0.1.0`, keeping all three packages on the same version initially |
| Initial documentation | Markdown in the repository, with READMEs that also work on PyPI/npm |

Initial registry checks returned HTTP 404 for PyPI's `jevaro`
and `jevaro-server` project endpoints and npm's `jevaro` endpoint. These names
were not reserved by those checks. Recheck npm before its first publication.

## 1. Establish the project identity

- [x] Choose one repository, its owner, the license, and the copyright holder.
- [x] Check the proposed package names against the public registries.
- [x] Add the Apache-2.0 license and a project notice at the root and in each package.
- [x] Add package authorship, license, and intended repository/documentation URLs.
- [x] Configure each package to include its license and notice in distributions.
- [x] Verify the resulting Python wheels/source archives and npm tarball in stage 3.

The metadata links point to the public repository.

## 2. Write the user and contributor documentation

- [x] Write the [README](../README.md) and [quickstart](quickstart.md).
- [x] Document the server, HTTP API, Arrow schema, and both SDKs.
- [x] Add a [contributor guide](../CONTRIBUTING.md) with setup and test commands.
- [x] Use source installation instructions until the registry packages exist.
- [x] Use absolute cross-package links in registry READMEs.
- [x] Keep earlier demos, graphics, and generated results in the ignored `local/` folder.
- [x] Verify source installs, examples, saved files, and offline tests in a clean checkout.

The examples passed against a local fake TypeSafe API using the installed
server command. Both Python interfaces, JavaScript, curl, and metadata-only
file decoding were checked. No paid API calls were made for this verification.

Installation instructions now use PyPI and npm.

## 3. Verify packages and add CI

- [x] Build two wheels, two source archives, and one npm tarball.
- [x] Check metadata, versions, licenses, code, typing files, examples, and entry points.
- [x] Install the archives in fresh environments outside the checkout.
- [x] Run Python, JavaScript, TypeScript, HTTP, and saved-file example checks.
- [x] Verify the declared Python and Node minimum versions.
- [x] Add [GitHub Actions](../.github/workflows/ci.yml) and validate the workflow.
- [x] Add a [changelog](../CHANGELOG.md) and [build/release guide](releasing.md).

Local archive checks passed on Python 3.11.14 with Node 20.3.0, and Python
3.14.6 with Node 24.8.0. Dependencies resolved and passed `pip check`. API
responses were simulated; no paid calls were made.

CI builds the five files once and tests those same files across Python
3.11/3.14 and Node 20.3.0/24. It checks the Python and JavaScript versions
separately so each registry can receive updates independently.

## 4. Publish the source repository

- [x] Review and commit the public source files.
- [x] Create and push the public `columnar-tech/jevaro` repository.
- [x] Set the description to `Jev + Arrow`, add topics, and enable issues.
- [x] Run the build and all four compatibility jobs on GitHub.

The [first CI run](https://github.com/columnar-tech/jevaro/actions/runs/36515834264)
passed. New pushes repeat the checks.

## 5. Rehearse the registry release

The [TestPyPI release](https://github.com/columnar-tech/jevaro/actions/runs/36519019374)
published `jevaro` and `jevaro-server` at `0.1.0` from commit
`8bf9db34d09f945eb78358121a1085b45bb13a2b`. All four Python files on TestPyPI
matched the tested CI artifacts by SHA-256.

Fresh TestPyPI installs passed the Python, JavaScript, TypeScript, HTTP, and
saved-file checks. Python dependencies came from ordinary PyPI; the JavaScript
package came from the tested tarball. No paid API calls were made.

The manual [TestPyPI workflow](../.github/workflows/testpypi.yml) and
[release guide](releasing.md#testpypi-rehearsal) document how to repeat this.

Trusted publishing is configured for both Python packages. The manual
[PyPI workflow](../.github/workflows/pypi.yml) is documented in the
[release guide](releasing.md#pypi-release).

The first npm upload used the maintainer account `ianmcook`. Later releases
use the configured GitHub trusted publisher. See the
[npm release instructions](releasing.md#npm-release).

Done when the maintainer can identify the exact source tag, version, and tested
artifacts that will be published, and the required registry setup is ready.

## 6. Publish and verify 0.1.0

[jevaro 0.1.0](https://pypi.org/project/jevaro/0.1.0/) and
[jevaro-server 0.1.0](https://pypi.org/project/jevaro-server/0.1.0/) were published
on 2026-09-29 by the
[PyPI workflow](https://github.com/columnar-tech/jevaro/actions/runs/36520364517),
from commit `36170aae06a783dfef32baa654938624d0a330f8`. All four published Python
files matched the tested CI artifacts by SHA-256.

Fresh PyPI installs passed the SDK, server, HTTP, and saved-file checks.
JavaScript checks used the CI tarball, and API responses were simulated.

[jevaro 0.1.0 on npm](https://www.npmjs.com/package/jevaro/v/0.1.0) was published
on 2026-09-29 from commit `a07da86835ad3c01f3909f75decc5878c816cc99`, using the
archive from [CI run 36522701715](https://github.com/columnar-tech/jevaro/actions/runs/36522701715).
The published SHA-512 integrity hash and SHA-1 matched the tested archive.

Fresh installs from both production registries passed the SDK, TypeScript,
HTTP, and saved-file checks together. The browser bundle also passed a Chrome
check for schema delivery, all three types, input order, and cancellation.
No paid API calls were made for these checks.

Release notes and downloads are in the
[v0.1.0 GitHub release](https://github.com/columnar-tech/jevaro/releases/tag/v0.1.0).

Use trusted publishing for subsequent releases from the configured GitHub
workflow. Build and test the artifacts before the upload job. Document what to
do if only some packages upload successfully: inspect registry state and reuse
the original artifacts where still publishable rather than blindly uploading
new builds under an already released version.

Done when users can follow the public README using only GitHub, PyPI, npm,
and their TypeSafe API key.

## Python 0.1.1

[jevaro 0.1.1](https://pypi.org/project/jevaro/0.1.1/) and
[jevaro-server 0.1.1](https://pypi.org/project/jevaro-server/0.1.1/) were published
on 2026-09-29 by [PyPI run 36524266654](https://github.com/columnar-tech/jevaro/actions/runs/36524266654),
from commit `2f5d90a7fce217fb75abdc6378c7837f21316e03`.

This updates the READMEs and short descriptions on PyPI. The published text
and all four file hashes match the tested archives. Runtime files are identical
to `0.1.0`. Fresh installs passed the SDK, TypeScript, HTTP, and saved-file checks
with the npm client at `0.1.0`.

Downloads are in the [v0.1.1 GitHub release](https://github.com/columnar-tech/jevaro/releases/tag/v0.1.1).

## 0.2.0

All three packages were published on 2026-09-29:
[Python SDK](https://pypi.org/project/jevaro/0.2.0/),
[server](https://pypi.org/project/jevaro-server/0.2.0/), and
[JavaScript SDK](https://www.npmjs.com/package/jevaro/v/0.2.0).

The [PyPI workflow](https://github.com/columnar-tech/jevaro/actions/runs/36584979604)
published from `5581a65b07d6fd1bc5674a0f22690e809a7fe28c`.
The [npm workflow](https://github.com/columnar-tech/jevaro/actions/runs/36585686389)
published from `110ed04308eefc7033a455ac96b8ea570436617d`, which fixes the
publishing command's archive path. Package contents are unchanged between
these commits. npm trusted publishing is now configured for `npm.yml` and
the `npm` environment.

All four compatibility jobs passed. Fresh installs from both registries
passed the SDK, TypeScript, HTTP, and saved-file checks. Published READMEs
match the source; all Python SHA-256 hashes and the npm archive's SHA-512
and SHA-1 hashes match the tested files. API responses were simulated.

The source tag and tested archives are in the
[v0.2.0 GitHub release](https://github.com/columnar-tech/jevaro/releases/tag/v0.2.0).

## 0.3.0

All three packages were published on 2026-10-05:
[Python SDK](https://pypi.org/project/jevaro/0.3.0/),
[server](https://pypi.org/project/jevaro-server/0.3.0/), and
[JavaScript SDK](https://www.npmjs.com/package/jevaro/v/0.3.0).
This release adds Arrow input.

The [PyPI workflow](https://github.com/columnar-tech/jevaro/actions/runs/37324111906)
and the [npm workflow](https://github.com/columnar-tech/jevaro/actions/runs/37324144555)
both published from `e0ca0254af125bd998197e335bfa178911c873e5`.

All four compatibility jobs passed in each workflow. Fresh installs from both
registries passed the SDK, TypeScript, HTTP, and saved-file checks on Python
3.11 and 3.14. Published READMEs match the source; all Python SHA-256 hashes
and the npm archive's SHA-512 and SHA-1 hashes match the tested files. API
responses were simulated for these checks.

Before release, paid live checks sent Arrow states through both SDKs and curl.
A 10,000-state run with Arrow input reached 457 states per second, in line
with 464 for JSON input.

The source tag and tested archives are in the
[v0.3.0 GitHub release](https://github.com/columnar-tech/jevaro/releases/tag/v0.3.0).

## References checked for this plan

- [PyPA: packaging projects and distribution metadata](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
- [PyPI: trusted publishing](https://docs.pypi.org/trusted-publishers/)
- [PyPI: creating a new project through a pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
- [npm: trusted publishing](https://docs.npmjs.com/trusted-publishers/)
- [npm: staging requires an existing package](https://docs.npmjs.com/staged-publishing/)
- [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
