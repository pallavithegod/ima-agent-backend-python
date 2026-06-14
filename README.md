# RecallOps Python Agent Backend

This FastAPI service is the incident intelligence and remediation engine for
RecallOps. It receives authenticated deployment sync requests, captures failed
Vercel and Render deployments, searches Hindsight for similar historical
errors, asks DeepSeek to diagnose and repair the issue, and prepares a
reviewable GitHub draft pull request.

## Responsibilities

- Verify RecallOps JWTs issued by the Node backend
- Fetch per-user provider credentials from the Node runtime endpoint
- Synchronize failed Vercel and Render deployments
- Capture deployment metadata, logs, repository, and commit SHA
- Retrieve the failed commit and changed files through GitHub MCP
- Search Hindsight before diagnosing a new error
- Classify and diagnose incidents with DeepSeek
- Generate corrected source and a unified diff
- Store failures, incidents, fixes, and PR metadata
- Create draft GitHub pull requests after explicit approval
- Expose analytics, on-call handoff, and PDF incident reports

## Agent workflow

The incident graph follows this order:

```text
classify -> recall similar memory -> diagnose -> persist
```

For deployment remediation, the wider workflow is:

1. Fetch failed deployment and build logs.
2. Save the raw failure to Hindsight.
3. Resolve the GitHub repository and failed commit.
4. Fetch the commit diff and relevant source file.
5. Search Hindsight for similar user-scoped errors and prior fixes.
6. Send current evidence plus retrieved memory to DeepSeek.
7. Store the incident and generated remediation.
8. Create a draft PR after user approval.

This ordering ensures prior resolutions are considered before generating a new
fix.

## Technology

- Python and FastAPI
- LangGraph
- DeepSeek through the OpenAI-compatible SDK
- Hindsight by Vectorize
- GitHub remote MCP
- SQLite
- ReportLab

## Requirements

- Python 3.11 or newer
- A running Node backend
- DeepSeek API key
- Hindsight API key and memory bank
- GitHub, Vercel, and Render credentials connected through the Node service

## Setup

Create a virtual environment and install dependencies:

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Start the API:

```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Open the health endpoint at `http://localhost:8000/health`. Interactive API
documentation is available at `http://localhost:8000/docs`.

If Windows blocks port 8000 with `WinError 10013`, use another port:

```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8010
```

Then update `VITE_API_URL` in the frontend.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `API_HOST` | Listening host |
| `API_PORT` | Local/default API port |
| `DATABASE_PATH` | SQLite incident database |
| `CORS_ORIGINS` | Comma-separated frontend origins |
| `AUTH_JWT_SECRET` | Verifies JWTs; must match the Node backend |
| `NODE_BACKEND_URL` | Node API used to retrieve per-user runtime credentials |
| `DEEPSEEK_API_KEY` | DeepSeek API credential |
| `DEEPSEEK_MODEL` | DeepSeek model name |
| `HINDSIGHT_API_URL` | Hindsight API base URL |
| `HINDSIGHT_API_KEY` | Hindsight API credential |
| `HINDSIGHT_BANK_ID` | Memory bank used for incident records |
| `VERCEL_TARGET` | Deployment target, normally `production` |
| `GITHUB_MCP_URL` | Official GitHub remote MCP endpoint |
| `GITHUB_DEFAULT_BRANCH` | Fallback repository branch |

`AUTH_JWT_SECRET` must be exactly the same as the Node backend value. All API
keys belong only in backend environment variables.

## Hindsight incident memory

RecallOps uses `https://api.hindsight.vectorize.io` and stores readable JSON
records for:

- Raw deployment failures
- Classified and diagnosed incidents
- Generated fixes
- Draft pull request URLs
- Resolution steps and timestamps

Records include provider, user, service, repository, commit, error evidence,
diagnosis, fix summary, rationale, and remediation result. Tags isolate recall
by user and make provider/repository filtering possible.

During recall, the service reconstructs Hindsight source chunks when a JSON
record is split across multiple chunks. Distilled memory facts remain available
as fallback context.

Configure:

```dotenv
HINDSIGHT_API_URL=https://api.hindsight.vectorize.io
HINDSIGHT_API_KEY=your-key
HINDSIGHT_BANK_ID=incident-memory-agent
```

## API overview

All `/api` endpoints require `Authorization: Bearer <RecallOps JWT>`.

### Health and reporting

- `GET /health`
- `GET /api/analytics`
- `GET /api/handoff`

### Incidents

- `GET /api/incidents`
- `GET /api/incidents/{incident_id}`
- `PATCH /api/incidents/{incident_id}/resolve`
- `GET /api/incidents/{incident_id}/report.pdf`
- `POST /api/incidents/{incident_id}/create-draft-pr`

Draft PR creation requires:

```json
{
  "approved": true
}
```

### Deployments

- `GET /api/deployments`
- `POST /api/integrations/vercel/sync`
- `POST /api/integrations/render/sync`

Sync endpoints accept an optional `limit` query parameter from 1 to 100.

## Deployment remediation details

### Vercel

The token must be able to access the project and its owning team. A Vercel
`403 Forbidden` is a provider permission problem, not a DeepSeek problem.
Rotate the token from the Connections page using a user who can see the target
project.

### Render

RecallOps reads the failed deploy's commit SHA, logs, and repository mapping.
The commit diff is included in diagnosis so the agent can compare the failing
commit with its parent.

### GitHub

The service uses the signed-in user's GitHub token through the official remote
MCP endpoint. The user must have read access to inspect source and write access
to create a branch and draft pull request.

## Tests

Run the test suite:

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

Run with coverage:

```powershell
.\venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing
```

## Azure App Service deployment

Deploy this folder as the application root.

- Startup command: `bash startup.sh`
- Persistent SQLite path: `/home/data/incidents.db`
- Enable App Service storage
- Enable build during deployment

The startup script runs:

```text
python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
```

Import `azure-app-settings.json` using Azure App Service's Advanced edit, but
replace all secret values first. Restart the service after changing settings.

Production URL:

```text
https://ima-python-g3dghmfzcxhwfwg2.canadacentral-01.azurewebsites.net
```

## Troubleshooting

### `DeepSeek key required`

Set `DEEPSEEK_API_KEY` on the Python service, not the Node service, and restart
Uvicorn or Azure App Service.

### `No repositories imported`

Import a GitHub repository in the frontend. Connecting GitHub alone does not
automatically start monitoring every repository.

### No Vercel project or logs are visible

The Vercel token must belong to a user with access to the project's team. Map
the project to the exact `owner/repository` name shown by GitHub.

### Hindsight is unavailable

Check `HINDSIGHT_API_URL`, API key, and bank ID. `/health` reports
`memory_mode: hindsight-cloud` when configured.

### The APIs reject the same login token differently

Ensure `AUTH_JWT_SECRET` is identical in both backend services and that their
clocks are correct.

### LangGraph deprecation warning

The current warning about serializer `allowed_objects` is non-fatal. It does
not prevent the API or tests from running.

## Data and security

- Incidents and deployments are scoped by authenticated user ID.
- Provider tokens are never stored in this service.
- The Node backend decrypts credentials only when building the authenticated
  runtime response.
- Hindsight recall is filtered by user tag to prevent cross-account memory
  retrieval.
- SQLite is intended for one Azure instance; use a managed database before
  scaling horizontally.
- Never commit `.env`, populated Azure settings, API keys, or service-account
  credentials.
