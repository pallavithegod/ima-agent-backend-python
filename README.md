# RecallOps Agent Backend

The single backend for RecallOps, a self-healing incident platform. It owns:

- **Auth** — email/password (scrypt) and GitHub sign-in via Firebase; HS256 JWTs.
- **Integrations** — GitHub repo import & activity, Vercel (OAuth PKCE or personal
  access token), Render; encrypted provider tokens (AES-256-GCM) in Postgres.
- **AI incident pipeline** — LangGraph classify → recall (Hindsight memory) →
  diagnose (DeepSeek), incident storage, analytics, on-call handoff, PDF reports.
- **Self-healing** — three sources of failure signals:
  1. **Project VMs** (primary): sidecar watchers on each app VM/container report
     crashes, failed health checks and heartbeats to `/api/vms/*`.
  2. **Vercel/Render** deployment sync.
  3. **Commit monitor**: polls imported GitHub repos and inspects deployments.
  On failure the agent diagnoses the incident, **clones the repo, generates a
  multi-file fix with the LLM, pushes a branch and opens a draft PR** using the
  user's GitHub token (single-file contents-API fallback if cloning fails).

The old Node backend (`ima-agent-backend-node`) is retired — everything it did
lives here now, minus the Firestore credential mirror (dropped intentionally).

## Local quickstart (Docker Desktop)

```bash
cp .env.example .env          # fill in AUTH_JWT_SECRET, DEEPSEEK_API_KEY,
                              # FIREBASE_SERVICE_ACCOUNT_BASE64, VITE_FIREBASE_*
docker compose up -d --build  # postgres, agent-backend, frontend, app-one, app-two
```

- Frontend: http://localhost:5173 · API: http://localhost:8000 · Postgres: localhost:5433
- Demo apps: flaky-shop http://localhost:3001, flaky-notes http://localhost:3002

### Registering the demo "project VMs"

1. Sign in on the frontend (or `POST /api/auth/register`), grab the JWT.
2. Push the demo apps to your own GitHub account (the fixer clones them there):
   ```bash
   cd demo-apps/flaky-shop  && git init && git add -A && git commit -m init && gh repo create flaky-shop --public --source . --push
   cd ../flaky-notes        && git init && git add -A && git commit -m init && gh repo create flaky-notes --public --source . --push
   ```
3. Register each VM (repeat for flaky-notes / app-two):
   ```bash
   curl -X POST http://localhost:8000/api/vms \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"name":"app-one","serviceName":"flaky-shop","repository":"<you>/flaky-shop"}'
   ```
4. Copy the returned `vm.id` / `apiKey` into `.env` (`APP_ONE_VM_ID`,
   `APP_ONE_VM_KEY`, `APP_TWO_VM_ID`, `APP_TWO_VM_KEY`), then
   `docker compose up -d app-one app-two`.
5. Sign in with GitHub (Firebase) once so the backend stores your GitHub token —
   the fixer uses it to push branches and open PRs.

### The demo

Open http://localhost:3001 and press **Checkout** — the app crashes, the sidecar
reports it and restarts the app, an incident appears in the dashboard with a
diagnosis, and a draft PR fixing `src/pricing.js` shows up on your GitHub fork.
A second identical crash within 10 minutes is deduplicated.

## Development (host, without Docker for the app)

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt
docker compose up -d postgres
alembic upgrade head
uvicorn app.main:app --reload --port 8000
pytest
```

Unit tests run on SQLite automatically (see `tests/conftest.py`) — no Postgres
needed for `pytest`.

## Architecture map

```
app/
  main.py               app factory, CORS, lifespan (starts monitors)
  config.py             pydantic-settings (.env)
  security.py           JWT issue/verify (HS256, aud incident-memory-api)
  db/models.py          SQLAlchemy schema (all tables)
  db/repo.py            data access layer
  routers/auth.py       /api/auth/*  (register, login, firebase, me)
  routers/incidents.py  /api/incidents*, /api/analytics, /api/handoff, /api/deployments
  routers/integrations.py  /api/integrations/*  (github/vercel/render, health, sync)
  routers/vms.py        /api/vms* (registry + heartbeat/event ingest)
  services/
    crypto.py           scrypt + AES-256-GCM (byte-compatible with old Node backend)
    firebase.py         Firebase ID-token verification
    credentials.py      encrypted provider-connection storage
    runtime.py          per-user decrypted tokens + Vercel refresh
    github_rest.py      GitHub REST client (commits, contents, PRs)
    fixer.py            clone-based fix agent (guardrailed multi-file fixes)
    llm.py              DeepSeek classify/diagnose/fix
    memory.py           Hindsight cloud memory
    remediation.py      deployment-failure remediation pipeline
    monitor.py          commit monitor loop
    vm_monitor.py       VM event pipeline + heartbeat sweeper
sidecar/watcher.py      supervisor shipped into project-VM containers
demo-apps/              flaky-shop (Express), flaky-notes (Flask)
alembic/                migrations (alembic upgrade head)
```

## Environment variables

See `.env.example`. Critical: `AUTH_JWT_SECRET` and `CREDENTIAL_ENCRYPTION_KEY`
must keep the values the Node backend used if you want existing sessions and
stored provider tokens to keep working.

## Azure later

The compose topology maps 1:1 to the target Azure setup: `agent-backend` → the
always-on agent VM, `app-one`/`app-two` → project VMs (watcher becomes a systemd
unit running the same `sidecar/watcher.py`), `postgres` → Azure Database for
PostgreSQL. Point `AGENT_URL` on each project VM at the agent VM.
