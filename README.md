# Jevaro

A Python batching proxy for [Jev](https://docs.typesafe.ai/) that returns
[Apache Arrow](https://arrow.apache.org/) streams, with Python and JavaScript SDKs.

Jevaro is an experiment in bulk inference: ask the same questions about many
independent states in one client request. The
[TypeSafe API](https://docs.typesafe.ai/api) evaluates one state per request.
Jevaro makes those calls concurrently, or packs many states into each call
with [`rows_per_call`](docs/http-api.md#pack-states-into-fewer-calls). It
converts the JSON answers into an Arrow IPC stream, in input order.

The schema arrives before the answers. Each state becomes a row and each
question a column. Arrow extension types preserve the meaning of Choice,
Noul, and Score, with shared labels and legends stored once in the schema.
The resulting Arrow data can go into pandas, Polars, DuckDB, and other Arrow
tools.

| Package | Purpose |
| --- | --- |
| [jevaro-server](jevaro-server/README.md) | Python batching proxy; ordered Arrow IPC streams |
| [jevaro](jevaro-python/README.md) for Python | Sync and async Arrow readers |
| [jevaro](jevaro-javascript/README.md) for JavaScript | Async Arrow readers in browsers and Node.js |

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
readers. The batching proxy handles concurrent upstream calls, retries,
ordering, and conversion from JSON to Arrow. See
[configuration and limits](jevaro-server/README.md#limits) for details.

States can be sent as JSON or as an Arrow table; see the
[HTTP API](docs/http-api.md#arrow-request). We’re exploring better adaptation
to evolving API rate limits and output record batch sizes. [Open an issue](https://github.com/columnar-tech/jevaro/issues)
to share ideas.

See the [release plan](docs/release-plan.md) for publication progress.

## License

[Apache-2.0](LICENSE). Copyright 2026 Columnar Technologies Inc.
