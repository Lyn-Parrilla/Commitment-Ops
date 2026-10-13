# CommitmentOps AI

Portfolio-oriented production foundation for **commitment and deadline accountability**.

The product answers:

> What did we promise? Who owns it? When is it due? What evidence do we have? Are we on track? What's missing? Who needs to act? What does management need to know?

The first use case is grant deliverable accountability. The domain stays general enough for compliance requirements, vendor contracts, customer implementations, regulatory reporting, and other commitment workflows.

The HTTP foundation and first three domain slices are in place: a
**deterministic accountability domain** plus an in-memory contract for source
intake, requirement discovery, human review, and validated learning feedback,
with durable SQL persistence for authoritative workflows. Dashboards, auth,
document extraction, and LLM workflows are not implemented yet.

## Architecture

| Concern | Choice |
| --- | --- |
| Language | Python 3.11+ |
| API | FastAPI |
| Validation | Pydantic / pydantic-settings |
| Data | SQLAlchemy 2.x; PostgreSQL production target; SQLite tests |
| Future auth and files | Supabase Auth and Storage |
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
    discovery.py     Source, candidate, completeness, review, learning signals
  persistence/       SQLAlchemy records and transactional workflow repository
  main.py            Application factory
migrations/          Alembic schema migrations
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

Progress aggregation, risk, permissions, and complete security auditing are
deferred.

## Persistence and audit foundation

Slice 3 persists the authoritative workflow aggregate:

- workflows and lifecycle status;
- requirements, ownership, supervisors, dates, targets, provenance, review
  metadata, status, and evidence expectations;
- referenced people, including owners, supervisors, reviewers, and evidence
  submitters; and
- evidence descriptions, types, references, submission timestamps, submitters,
  and status.

Domain models remain independent of SQLAlchemy. `WorkflowRepository` translates
between the existing Pydantic domain and relational records. It supports saving
one complete workflow aggregate, retrieving by identifier, listing all
workflows, and filtering by workflow type or status. Missing identifiers return
`None`. The repository rejects duplicate or inconsistent requirement and
evidence associations rather than silently reassigning them.

A workflow and all related people, requirements, evidence, and its audit event
are committed in one transaction. A failed record or audit insert rolls back
the entire save. Persisted identifiers and review state are reconstructed
without promoting pending AI-inferred requirements.

PostgreSQL is the production target and is compatible with Supabase PostgreSQL.
The standard suite uses isolated SQLite files and injects its migration URL
within the test, so it never uses an externally configured production URL.
Application startup also does not connect automatically.

Set `DATABASE_URL` only through local or deployment environment configuration.
Conventional `postgresql://` provider URLs are normalized to the psycopg 3
driver. Apply migrations with:

```bash
alembic upgrade head
```

The Docker image includes `alembic.ini` and the complete `migrations/` tree.
Building, migrating, and starting are separate operations:

```bash
# Build the image.
docker build -t commitmentops .

# In a shell where DATABASE_URL has been exported, stop if it is unset.
: "${DATABASE_URL:?Export DATABASE_URL before running migrations}"

# Deliberately apply migrations.
docker run --rm -e DATABASE_URL commitmentops alembic upgrade head

# Start the application; startup does not run migrations.
docker run --rm -p 8000:8000 -e PORT=8000 commitmentops
```

Export `DATABASE_URL` before running the migration command. The command
deliberately receives that existing environment value without placing a
connection string in source or shell history. The image's default command starts
only uvicorn; it does not apply migrations automatically.

Railway should supply `DATABASE_URL` through service variables. Alembic uses
that same value; no connection string or credential belongs in Git. Production
schema changes must use migrations rather than runtime table creation.

Every successful repository save appends a workflow audit event in the same
transaction. Events contain an event identifier and UTC timestamp, workflow
identifier, create/update operation, bounded before/after accountability
snapshots, and optional caller-supplied actor and correlation references. The
repository exposes audit history as read-only and provides no update or delete
operation.

This history records persistence activity; it is not tamper-proof security
logging, authentication, authorization, multi-tenant isolation, or evidence of
regulatory compliance. Actor identity is never inferred.

## Learning loop 1 — document and requirement intelligence

CommitmentOps treats source documents as evidence about potential commitments,
not as organizational truth. Slice 2 implements this deterministic, in-memory
flow:

```text
Grant document
  -> candidate requirement discovery
  -> completeness assessment
  -> human review
  -> authoritative Requirement after approval
  -> validated feedback signal
```

A `RequirementCandidate` and a `Requirement` are deliberately different
objects. Every candidate remains non-authoritative, including
`document_explicit` candidates. Approval is the only candidate operation that
creates an existing Slice 1 `Requirement`, and it records the human approval on
that requirement. Rejection creates no requirement. AI-inferred candidates
therefore cannot bypass the existing human-approval protection.

Candidate completeness is deterministic. Title, description, owner, and
evidence expectation are required to cross the authority boundary. Due date,
target, and supervisor are tracked as optional accountability information.
Missing values are reported; they are never invented.

Source traceability retains the source document, any known page/section/heading
or other source reference, provenance, discovery reason, confidence, and
requirement-versus-deliverable classification. Unknown source details stay
unset.

Each human approval or rejection emits a `LearningSignal` alongside the review
decision. It contains immutable snapshots of:

- what discovery originally predicted;
- what the human actually reviewed, including corrections;
- the source and discovery metadata;
- the human decision and reviewer; and
- the resulting requirement characteristics, when approval created one.

A human-added requirement that discovery missed can also be recorded as a
learning signal. Learning signals are feedback about system performance; they
are not requirements and cannot approve requirements. The original discovery
remains separate from the reviewed or corrected candidate and the human
decision, so later evaluation can distinguish AI output from validated human
feedback.

The optional organization reference on source metadata keeps validated feedback
groupable for future organization-specific terminology, formats, evidence
patterns, and workflow conventions.

Source documents, candidates, discovery snapshots, and learning signals remain
in memory and are not durable across process restarts. Slice 3 persists only the
authoritative accountability aggregate. The discovery slice captures feedback;
it does not train a model or implement embeddings, vector databases,
retrieval-augmented generation (RAG), fine-tuning, or autonomous retraining.
Future learning must continue to use validated human outcomes rather than
unreviewed AI output.

## Learning loop 2 — personalized workflow intelligence (planned)

This loop is confirmed future architecture; it is not implemented in the
current code.

The planned workflow-intelligence loop will:

- observe relevant workflow events, deliverable progress, evidence collection,
  submissions, communications, responses, blockers, and deadlines;
- identify potential individual work patterns and organizational bottlenecks
  from recorded evidence;
- preserve observations, hypotheses, confidence, recommendations,
  interventions, approvals, and measured outcomes as distinct concepts;
- recommend personalized assistance based on observed evidence and prior
  outcomes; and
- keep workflow-intelligence feedback logically separate from document and
  requirement discovery feedback.

An engagement strategy must not change automatically. Any proposed change must
receive explicit human approval first. The future system must record the
proposed change, approver, decision, decision timestamp, executed change, and
subsequent outcome so recommendations can be evaluated without conflating a
proposal with an action or result.

This loop must avoid invasive surveillance, unsupported conclusions about
employee motivation, punitive profiling, and treating individual behavior as
the explanation for systemic process problems. Recommendations must remain
evidence-based, reviewable, and subordinate to human judgment.

## Reporting and approval architecture (planned)

The following capabilities are confirmed requirements for future slices and do
not exist in the current implementation:

- weekly operational reports and monthly management reports;
- multiple authorized email recipients per workflow, including recipient
  management;
- multiple designated engagement approvers;
- configurable approval policies supporting either one authorized approver or
  all designated approvers; and
- enforcement that no proposed engagement change executes until its applicable
  approval condition is satisfied.

Future reports should cover deliverables, owners, deadlines, outstanding and
overdue work, evidence, submissions, communication history, responses, emerging
patterns, recommendations, and approval decisions. Communication reporting
should distinguish attempted, sent, delivered, and acknowledged states whenever
the underlying channel makes those states available.

Reports must be versioned and access-controlled, with generation timestamps,
source-data references, authorized recipients, and delivery outcomes. Scheduled
generation, email delivery, behavioral analysis, recipient management, and
multiple-approver workflows are planned only; none are implied by the current
domain models.

## Local setup

Requires Python 3.11+.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` locally when integrations need credentials. Do not put real keys in source, tests, or docs.
Set `DATABASE_URL` before running Alembic or application persistence operations.

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

Application startup does not require integration credentials. `DATABASE_URL` is
required when running migrations or persistence operations; the remaining
integration values are reserved for later slices.

| Variable | Purpose |
| --- | --- |
| `ENVIRONMENT` | `development`, `test`, or `production` |
| `LOG_LEVEL` | Process log level |
| `PORT` | HTTP port (Railway sets this; local default is 8000) |
| `DATABASE_URL` | PostgreSQL connection URL for persistence and Alembic |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Browser / user-scoped Supabase key |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only Supabase key; never expose to clients |
| `LLM_API_KEY` | Provider credential for structured LLM calls |
| `LLM_API_BASE_URL` | Optional provider base URL |

## Deployment

Railway can build from the `Dockerfile`. The container starts uvicorn on `$PORT` and expects `/health` as the liveness path. Supply the same environment variables in the Railway service settings — not in Git.

## What this foundation does not include

Dashboard UI, authentication and authorization, tenant isolation, production
Supabase provisioning, persistence for discovery/learning objects, grant
parsing, document extraction, CSV ingestion, LLM client code, embeddings,
recognition engines, RAG, model training/retraining, scheduled reports, email
delivery, behavioral analysis, and multiple-approver workflows. Those belong in
subsequent, test-backed slices.
