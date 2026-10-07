# CommitmentOps AI

Portfolio-oriented production foundation for **commitment and deadline accountability**.

The product answers:

> What did we promise? Who owns it? When is it due? What evidence do we have? Are we on track? What's missing? Who needs to act? What does management need to know?

The first use case is grant deliverable accountability. The domain stays general enough for compliance requirements, vendor contracts, customer implementations, regulatory reporting, and other commitment workflows.

The HTTP foundation is in place. The first product slice is a **deterministic in-memory domain** for workflows, requirements, owners, evidence, provenance, and human review. Dashboards, auth, persistence, document extraction, and LLM workflows are not implemented yet.

## Architecture

| Concern | Choice |
| --- | --- |
| Language | Python 3.11+ |
| API | FastAPI |
| Validation | Pydantic / pydantic-settings |
| Data, auth, files | Supabase (PostgreSQL, Auth, Storage) — configured later via environment |
| AI | LLM API with structured outputs — integrated later |
| Deploy | Railway, hosted desktop web application |
| Source | GitHub |

Design rules:

- Deterministic business rules stay in application code, not in the model.
- AI may interpret, extract, classify, summarize, and recommend. It must not silently invent an organizational requirement.
- AI-discovered requirements need provenance and human review before they become authoritative.
- Secrets live in environment configuration. They are never committed.

## Repository layout

```
app/                 FastAPI application
  api/health.py      Liveness endpoint
  config.py          Environment-backed settings
  domain/            Deterministic accountability model (no HTTP)
  main.py            Application factory
tests/               Pytest suite
.env.example         Documented environment variable names (no secrets)
Dockerfile           Container image for Railway
```

## Domain

`app.domain` is independent of FastAPI. Authoritative state is created and transitioned in Python:

| Concept | Role |
| --- | --- |
| Workflow | Named effort (grant, compliance, vendor, implementation, reporting, or general) with a lifecycle |
| Requirement | Promised work: owner, optional supervisor/due date/target, evidence expectation, provenance |
| Person | Accountable human (owner, supervisor, reviewer, submitter) |
| Evidence | Submitted proof associated with a requirement |
| Provenance | How the requirement was introduced (`organization_template`, `document_explicit`, `ai_inferred`, `user_provided`, `system_derived`) |
| Review | Human approval gate; AI-inferred requirements cannot start as authoritative |

Progress aggregation, risk, permissions, audit logs, and persistence are deferred.

## Local setup

Requires Python 3.11+.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` locally when integrations need credentials. Do not put real keys in source, tests, or docs.

## Development commands

Run the API:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- Health: `GET http://127.0.0.1:8000/health`
- OpenAPI docs: `http://127.0.0.1:8000/docs` (disabled when `ENVIRONMENT=production`)

Run tests:

```bash
pytest
```

## Configuration

All values are optional for this foundation. Names are stable so later features can bind to them without renaming.

| Variable | Purpose |
| --- | --- |
| `ENVIRONMENT` | `development`, `test`, or `production` |
| `LOG_LEVEL` | Process log level |
| `PORT` | HTTP port (Railway sets this; local default is 8000) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Browser / user-scoped Supabase key |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only Supabase key; never expose to clients |
| `LLM_API_KEY` | Provider credential for structured LLM calls |
| `LLM_API_BASE_URL` | Optional provider base URL |

## Deployment

Railway can build from the `Dockerfile`. The container starts uvicorn on `$PORT` and expects `/health` as the liveness path. Supply the same environment variables in the Railway service settings — not in Git.

## What this foundation does not include

Dashboard UI, authentication screens, database schema, grant parsing, document extraction, CSV ingestion, and LLM client code. Those belong in subsequent, test-backed slices.
