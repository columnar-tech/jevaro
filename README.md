# Jevaro

Evaluate many states with [TypeSafe](https://docs.typesafe.ai/) and read the
answers as an [Apache Arrow](https://arrow.apache.org/) stream.

Send one set of questions and a list of states. Jevaro calls TypeSafe for each
state, sends the Arrow schema first, and streams answer rows in input order.
Choice labels and Score legends are stored once in the schema.

| Package | Purpose |
| --- | --- |
| [jevaro-server](jevaro-server/README.md) | Python HTTP proxy |
| [jevaro](jevaro-python/README.md) for Python | Sync and async Arrow readers |
| [jevaro](jevaro-javascript/README.md) for Node.js | Async Arrow readers; ESM, CommonJS, and TypeScript |

## Start here

You need Python 3.11+ and a [TypeSafe API key](https://console.typesafe.ai).
Commands use a Unix shell.

Clone the example scripts, then install the Python packages from PyPI:

```sh
git clone https://github.com/columnar-tech/jevaro.git
cd jevaro
python3 -m venv .venv
.venv/bin/python -m pip install jevaro-server jevaro
export TYPESAFE_API_KEY="your-api-key"
.venv/bin/jevaro-server
```

In a second terminal at the repository root:

```sh
.venv/bin/python jevaro-python/example.py --output jevaro-results.arrows
.venv/bin/python scripts/read_results.py jevaro-results.arrows
```

This evaluates three customer messages, saves their answers, and prints the
full answers using the file's schema metadata. The reader script needs only
PyArrow. Each evaluation uses the TypeSafe API and incurs its normal charges.

Follow the [quickstart](docs/quickstart.md) to inspect the schema and run the
same example from JavaScript.

## Documentation

- [Quickstart](docs/quickstart.md)
- [Server configuration](jevaro-server/README.md)
- [HTTP API](docs/http-api.md)
- [Arrow schema](docs/arrow-schema.md)
- [Python SDK](jevaro-python/README.md)
- [JavaScript SDK](jevaro-javascript/README.md)
- [Contributing](CONTRIBUTING.md)
- [Build and test a release](docs/releasing.md)

Jevaro uses the official TypeSafe question helpers. Its SDKs return Arrow
readers. The server makes parallel upstream calls and uses the TypeSafe SDK's
retry policy. [Configuration and limits](jevaro-server/README.md#limits)
describe this first version's behavior.

See the [release plan](docs/release-plan.md) for publication progress.

## License

[Apache-2.0](LICENSE). Copyright 2026 Columnar Technologies Inc.
