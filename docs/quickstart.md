# Quickstart

Start Jevaro, evaluate three customer messages, and read the Arrow results
from Python and JavaScript.

You need Python 3.11+, Node.js 20.3+ for the JavaScript example, and a
[TypeSafe API key](https://console.typesafe.ai). Jevaro is preparing its first
release, so these instructions install from source. Use a Unix shell.
If you already have a checkout, start at its root and skip the clone commands.

## 1. Start the server

```sh
git clone https://github.com/columnar-tech/jevaro.git
cd jevaro
python3 -m venv .venv
.venv/bin/python -m pip install -e ./jevaro-server -e ./jevaro-python
export TYPESAFE_API_KEY="your-api-key"
.venv/bin/jevaro-server
```

Leave this terminal running. The server listens on `http://127.0.0.1:8000`.
It supplies the API key for requests that do not send their own.

## 2. Evaluate three states from Python

Open a second terminal at the repository root:

```sh
export TYPESAFE_BASE_URL="http://127.0.0.1:8000"
.venv/bin/python jevaro-python/example.py --output jevaro-results.arrows
```

The [example](../jevaro-python/example.py) sends a refund request, a missing
parcel report, and a duplicate charge report. All three use these questions:

| Question | Type | Result |
| --- | --- | --- |
| `department` | Choice | Which team should handle the message |
| `urgency` | Score | How soon the customer wants a resolution |
| `refund` | Noul | Probability that the customer requests a refund |

The script prints three JSON lines and saves the full answers in
`jevaro-results.arrows`. `--output` replaces that file if it exists. Results
stay in the same order as the states.

## 3. Inspect the saved schema

```sh
.venv/bin/python - <<'PY'
import json
import pyarrow as pa

with pa.ipc.open_stream("jevaro-results.arrows") as reader:
    for field in reader.schema:
        print(field.name, field.type)
        print(json.loads(field.metadata[b"ARROW:extension:metadata"]))
    print("Rows:", sum(batch.num_rows for batch in reader))
PY
```

You should see three columns and `Rows: 3`. The Choice labels and Score
legend appear once in the schema. The first record batch is empty; it lets
the schema arrive before any TypeSafe answers.

To print full answer objects, including named probabilities:

```sh
.venv/bin/python read_results.py jevaro-results.arrows
```

This reads only the file. It needs PyArrow, with no API key or original
question definitions. See the [Arrow schema](arrow-schema.md) for the format.

## 4. Run the JavaScript example

With the server still running:

```sh
npm --prefix jevaro-javascript ci
node jevaro-javascript/example.mjs
```

The [JavaScript example](../jevaro-javascript/example.mjs) uses the same three
states and questions. It streams the results and resolves Choice labels from
the schema. This makes three new evaluations; running both examples makes
six evaluations in total and incurs TypeSafe's normal API charges.

## Next

Edit `STATES` and `QUESTIONS` in the Python example, or `states` and `questions`
in the JavaScript example. Use `state` for one evaluation and `states` for many.
Stop the server with Ctrl-C when finished.

- [Python SDK](../jevaro-python/README.md)
- [JavaScript SDK](../jevaro-javascript/README.md)
- [Server configuration](../jevaro-server/README.md)
- [HTTP API](http-api.md)

If a stream fails, see [errors](http-api.md#errors). A response with status 200
can still fail while the body is being read.
