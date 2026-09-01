# Flask HTTP E2E

This test drives a running Waypoint service over HTTP. It imports Flask, verifies known
architecture, checks the persistent index, reads source, and inspects the `flask.app.Flask`
symbol and its graph neighborhood.

Start Waypoint, then run:

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.http_flask_e2e
```

For a local Flask checkout inside `ONBOARD_ALLOWED_ROOT`:

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.http_flask_e2e `
  --repository-path ".waypoint-clones\pallets--flask--YOUR_ID"
```

The result is written to `verification/results/http-flask-e2e.json`. This path makes real
HTTP requests but does not call an LLM, so it proves the application pipeline separately
from provider behavior.
