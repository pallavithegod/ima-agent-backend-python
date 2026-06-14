# Python Agent Backend

FastAPI and LangGraph service for real Vercel-to-GitHub remediation.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## Required live credentials

- Vercel and GitHub credentials are supplied per user by the Node authentication service.
- `GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/`
- `DEEPSEEK_API_KEY` for diagnosis and corrected source generation

Copy `.env.example` to `.env` and keep the JWT secret identical to `node-backend/.env`.

## Live workflow

1. `POST /api/integrations/vercel/sync` lists failed production deployments from the configured Vercel project.
2. The deployment detail and build events provide the commit SHA and error logs.
3. GitHub's official remote MCP server returns commit changes and source content.
4. LangGraph classifies and diagnoses the incident.
5. DeepSeek generates a complete corrected file and unified diff.
6. `POST /api/incidents/{id}/create-draft-pr` creates a branch and draft PR through GitHub MCP after explicit approval.

No incidents are seeded. The database starts empty and stores only real synchronized or manually submitted events.
