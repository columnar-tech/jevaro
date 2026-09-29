# Contributing to Jevaro

## Setup

Use Python 3.11+ and Node.js 20.3+. Run these commands from the repository root
in a Unix shell:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ./jevaro-server -e ./jevaro-python
npm --prefix jevaro-javascript ci
```

The code lives in three directories:

| Directory | Contents |
| --- | --- |
| `jevaro-server/src/jevaro_server` | HTTP proxy and Arrow types |
| `jevaro-python/src/jevaro` | Python client and readers |
| `jevaro-javascript` | JavaScript client and TypeScript declarations |

## Tests

```sh
.venv/bin/python -m unittest discover -s jevaro-python/tests -v
.venv/bin/python -m unittest discover -s jevaro-server/tests -v
npm --prefix jevaro-javascript test
npm --prefix jevaro-javascript run typecheck
```

These tests need no TypeSafe key and make no paid API calls. The server suite
opens local HTTP ports and tests both SDKs against a fake upstream. Running
`npm test` alone skips the four HTTP integration tests; the server suite runs
them.

For an optional live check, set `TYPESAFE_API_KEY` and run:

```sh
.venv/bin/python jevaro-server/tests/live_smoke.py
```

This evaluates three states through each SDK, checks a temporary Arrow file,
and stops its server. It makes six paid evaluations, plus any upstream retries.

## Changes

Keep examples small and writing plain. Update documentation when changing
request arguments, stream behavior, or configuration. Check the live
[TypeSafe docs](https://docs.typesafe.ai/llms.txt) before changing an upstream
integration.

For protocol changes, check ordering, precision, metadata, cancellation, and
incomplete streams in both languages.

Before submitting a change, run the relevant tests and describe what changed
and how it was checked. Keep API keys and generated results out of commits.

## Releases and license

See [Build and test a release](docs/releasing.md) for archive checks and version
updates. CI installs the built packages and tests both SDKs against the proxy.
The first-release work is tracked in the [release plan](docs/release-plan.md).

Jevaro uses [Apache-2.0](LICENSE). Copyright 2026 Columnar Technologies Inc.
The bundled TypeSafe skill uses its own [MIT license](.agents/skills/typesafe-ai/LICENSE).
