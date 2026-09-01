# Deployment constraints

The React frontend can be deployed from the `frontend` subdirectory of a GitHub repository.
That does not require moving the backend. The hosting project must set its root directory to
`frontend` and point the frontend API base URL at a separately deployed backend.

The full Waypoint backend is not a natural serverless frontend deployment because it uses:

- long-running repository analysis;
- `git clone` subprocesses;
- a writable clone directory;
- persistent SQLite indexes, sessions, and conversations;
- trace files and potentially large repositories;
- optional locally authenticated Claude Agent SDK access.

A production deployment should therefore split the static frontend from a persistent
container/service backend. Docker is not the only acceptable deployment, but it is the
simplest reproducible local containerization artifact for the internship rubric. A hosted
container on Render, Railway, Fly.io, Azure, AWS, or similar is also valid when it provides
persistent storage and sufficient request/runtime limits.

## Recommended deployment

For a public demo, deploy `frontend` to Vercel and deploy the backend container to a service
with a persistent disk. Set `VITE_API_BASE_URL` during the Vercel build to the backend's HTTPS
origin, and set `WAYPOINT_CORS_ORIGINS` on the backend to the Vercel origin. A locally logged-in
Claude Code session will not transfer to a hosted container; use OpenRouter or an Anthropic API
key in hosted environments.

For the most reproducible mentor review, the included Compose deployment is simpler:

```powershell
docker compose up --build
```

Then open `http://127.0.0.1:5173`. Named volumes preserve SQLite, traces, indexes, and managed
GitHub clones across container restarts. Compose validates successfully with
`docker compose config --quiet`.
