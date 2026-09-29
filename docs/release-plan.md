# Jevaro release plan

This is the working plan for the first public release. The repository and
registry packages have not been published as part of this preparation.

Documentation, release files, and CI are ready. Local package checks pass.
Next: prepare the public repository and run CI on GitHub in stage 4.

## Decisions

| Item | Decision |
| --- | --- |
| Source repository | One repository: `columnar-tech/jevaro` |
| License | Apache-2.0 |
| Copyright holder | Columnar Technologies Inc. |
| Server distribution | PyPI: `jevaro-server`; command: `jevaro-server` |
| Python SDK distribution | PyPI: `jevaro`; import: `jevaro` |
| JavaScript SDK distribution | npm: `jevaro`; import/require: `jevaro` |
| Proposed first release | `0.1.0`, keeping all three packages on the same version initially |
| Initial documentation | Markdown in the repository, with READMEs that also work on PyPI/npm |

Registry checks in this planning session returned HTTP 404 for PyPI's `jevaro`
and `jevaro-server` project endpoints and npm's `jevaro` endpoint. These names
are not currently listed publicly; the checks do not reserve them or guarantee
that publication will accept them. Recheck before publishing. GitHub's public
API also returned 404 for `columnar-tech/jevaro`; authenticated access should be
checked before creating it, since a private repository can return the same code.

## 1. Establish the project identity

- [x] Choose one repository, its owner, the license, and the copyright holder.
- [x] Check the proposed package names against the public registries.
- [x] Add the Apache-2.0 license and a project notice at the root and in each package.
- [x] Add package authorship, license, and intended repository/documentation URLs.
- [x] Configure each package to include its license and notice in distributions.
- [x] Verify the resulting Python wheels/source archives and npm tarball in stage 3.

The metadata links point to the intended public repository. They will become
live after the GitHub publication stage.

## 2. Write the user and contributor documentation

- [x] Write the [README](../README.md) and [quickstart](quickstart.md).
- [x] Document the server, HTTP API, Arrow schema, and both SDKs.
- [x] Add a [contributor guide](../CONTRIBUTING.md) with setup and test commands.
- [x] Use source installation instructions until the registry packages exist.
- [x] Use absolute cross-package links in registry READMEs.
- [x] Preserve the [earlier examples](earlier-examples.md); ignore generated data and results.
- [x] Verify source installs, examples, saved files, and offline tests in a clean checkout.

The examples passed against a local fake TypeSafe API using the installed
server command. Both Python interfaces, JavaScript, curl, and metadata-only
file decoding were checked. No paid API calls were made for this verification.

Review the public file list and first commit in stage 4. Switch the user
installation instructions to PyPI and npm after publication.

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
3.11/3.14 and Node 20.3.0/24. The first hosted run remains part of stage 4,
after pushing the source to GitHub. All packages remain unreleased at `0.1.0`.

## 4. Publish the source repository

Initialize the chosen public source layout as a Git repository, review its first
commit, and create/push `columnar-tech/jevaro`. Set the description and topics,
enable the issue tracker, and run CI against that commit. Confirm package and
documentation links resolve. The prepared file list and first commit are the
reviewable result before making the repository public.

The maintainer will need a GitHub account with permission to create repositories
in `columnar-tech`. Check existing access before asking for any new setup.

## 5. Rehearse the registry release

Confirm maintainer access to PyPI, TestPyPI, and npm. These account identities
are separate from GitHub organization ownership. Use the registry's normal
login/2FA flow when needed.

Upload the Python distributions to TestPyPI and install them into a fresh
environment. Install their dependencies from ordinary PyPI separately; fetch
Jevaro itself from TestPyPI with `--no-deps`. For JavaScript, install and test
the packed tarball in a fresh project before the first registry upload.

Prepare GitHub Actions trusted publishing for PyPI, which supports a pending
publisher for a new project. Plan the first npm upload through an authenticated
maintainer; then configure the package's trusted publisher for later releases.
npm's optional staging feature requires an already existing package, so it is
not the rehearsal mechanism for Jevaro's first npm upload.

Done when the maintainer can identify the exact source tag, version, and tested
artifacts that will be published, and the required registry setup is ready.

## 6. Publish and verify 0.1.0

Publish `jevaro-server` and `jevaro` to PyPI and `jevaro` to npm. Install each by
its public name in a fresh environment, verify imports and the server command,
and run a small end-to-end example. Add release notes and package links to the
GitHub release and README.

Use trusted publishing for subsequent releases from the configured GitHub
workflow. Build and test the artifacts before the upload job. Document what to
do if only some packages upload successfully: inspect registry state and reuse
the original artifacts where still publishable rather than blindly uploading
new builds under an already released version.

Done when users can follow the public README using only GitHub, PyPI, npm,
and their TypeSafe API key.

## References checked for this plan

- [PyPA: packaging projects and distribution metadata](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
- [PyPI: trusted publishing](https://docs.pypi.org/trusted-publishers/)
- [PyPI: creating a new project through a pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
- [npm: trusted publishing](https://docs.npmjs.com/trusted-publishers/)
- [npm: staging requires an existing package](https://docs.npmjs.com/staged-publishing/)
- [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
