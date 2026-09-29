# Jevaro browser example

A single [HTML file](index.html) with editable JSON states and questions.
Submit a batch and watch the Arrow results arrive in a table. Expand Choice
and Score cells for confidence, probabilities, and the Score legend.
Labels and legends come from the Arrow schema metadata.

The script and styles are in the page. It loads `jevaro@0.1.0` from
[esm.sh](https://esm.sh/); no build step is needed.

## Run

Use Python 3.11+, Node.js with `npx`, and a modern browser. Run these commands
from the repository root.

In one terminal, install and start Jevaro. This wrapper enables
[CORS](https://fastapi.tiangolo.com/tutorial/cors/) for the page on port 3000.
If Jevaro is already running on port 8000, stop it first with Ctrl+C and
restart it with this block. The plain `jevaro-server` command does not
enable CORS. The API key stays on the server.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install jevaro-server
export TYPESAFE_API_KEY="your-api-key"
.venv/bin/python - <<'PY'
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from jevaro_server.app import app

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)
uvicorn.run(app, host="127.0.0.1", port=8000)
PY
```

In a second terminal:

```sh
npx serve --listen tcp://127.0.0.1:3000 --no-port-switching jevaro-javascript/browser
```

Open **http://127.0.0.1:3000** and click **Submit**. The three sample states
use the same Choice, Score, and Noul questions as the SDK example. Submitting
sends real requests to Jev using `jev-latest`.

Rows stay in input order. **Cancel** stops the request and keeps any rows
already displayed. Errors also keep partial results and report the row count.

Internet access is needed to load the SDK from the CDN. If you change the
page's port, update the server's allowed origins to match. The server URL
field takes a base URL, without `/v1/systemone`.

An “access control checks” or CORS error usually means the server was started
without the wrapper above, or the page's origin differs from the allowed
origins. Open the page through `npx serve`, not directly as a local file.
