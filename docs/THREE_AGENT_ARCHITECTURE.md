# CommitmentOps AI three-agent architecture

This is the detailed product architecture for CommitmentOps AI. The README summarizes it. When the two disagree, this document and the current source code decide what is implemented.

The product answers one accountability question through three agents:

> What did we promise? Who owns it? When is it due? What evidence do we have? Are we on track? What's missing? Who needs to act? What does management need to know?

The first use case is grant deliverable accountability. Workflow types also cover compliance, vendor contracts, customer implementations, regulatory reporting, and general commitments.

## How to read status

| Status | Meaning |
| --- | --- |
| Implemented | Present in the current application and covered by the existing domain, API, and tests |
| Partial foundation | A real, bounded capability exists. The broader product behavior is still planned |
| Planned | Not in the application. Documentation must not describe it as available |

## The three agents

| Agent | Name | Job | Status |
| --- | --- | --- | --- |
| 1 | Document Intelligence | Read a grant or contract and propose requirements for a person to approve | Implemented for the supported PDF and DOCX path |
| 2 | Behavioral Intelligence | Notice work that is overdue or blocked and record a human decision, execution, and outcome | Partial foundation |
| 3 | Deliverable Verification | Receive submitted evidence and store files privately; AI verification and acceptance are not yet implemented. | Partial foundation |

The agents share one workflow, one organization boundary, and one human-approval rule. An agent may recommend. It does not make an authoritative requirement or an engagement change by itself.

## Agent 1 — Document Intelligence

An authorized organization member uploads a grant agreement, contract, or related PDF or DOCX. The application stores the file, extracts bounded text, asks a configured language model for structured candidates, assesses completeness, and waits for a person to approve, reject, or correct each candidate.

### Implemented

- **Document intake.** `POST /api/v1/workflows/{workflow_id}/source-documents` accepts PDF and DOCX only. The server builds the storage key `sources/{organization}/{workflow}/{document}`. A caller-supplied storage key is rejected. The upload response does not include the storage key, checksum, or document text.
- **Extraction.** `extract_document_text` reads supported PDF and DOCX bytes. It keeps page text when the format provides it and does not invent missing text. Output is bounded by file size, PDF object count, decompressed stream size, compression ratio, DOCX entry limits, page count, and page length.
- **Structured interpretation.** When `LLM_API_KEY` and `LLM_API_BASE_URL` are set, interpretation uses an OpenAI-compatible `POST /chat/completions` call with a JSON response. The model defaults to `gpt-4o-mini` and can be changed with `LLM_MODEL`. Temperature is 0. The model may label a candidate `document_explicit` or `ai_inferred`. Invalid model output creates no candidates and fails the document. A missing key or base URL fails the document with `interpretation_unavailable`. Transient timeout, 429, and 5xx responses are retried once.
- **Candidate discovery.** Each interpreted obligation becomes a non-authoritative `RequirementCandidate` with provenance, classification, source location, discovery reason, and optional confidence. Candidates are persisted with the source document.
- **Completeness assessment.** Title, description, owner, and evidence expectation are required before approval can create a requirement. Due date, target, and supervisor are optional. Missing values are reported and are not invented.
- **Human approval.** The reviewer is the verified JWT subject, not a body field. Approval is the only candidate operation that adds a workflow requirement. Rejection creates no requirement. A person can revise a candidate and can record a requirement the reading missed. Each review stores a separate learning signal with immutable snapshots of the original discovery, the reviewed candidate, the source, the decision, and any resulting requirement.

### Partial

The PDF reader is a bounded parser in this repository, not a general PDF library. A stream with a plain zlib header is inflated under the output cap. Literal PDF text is kept. Many production PDFs use raw FlateDecode and can yield little or no text. DOCX reading is limited to `word/document.xml`. Long documents are truncated before they are sent to the model, and the truncation is recorded. There is no CSV ingestion.

### Planned, and not implemented

Claude is not integrated. The interpreter is not a Claude client. Retrieval-augmented generation, embeddings, vector search, fine-tuning, model training, and autonomous retraining are not implemented. Learning signals are evaluation records. They do not update a model.

## Agent 2 — Behavioral Intelligence

This agent watches persisted requirement facts and proposes a next step. It does not infer motivation, change an owner, or send a message.

### Implemented

`detect_conditions` proposes `overdue_requirement` and `blocked_requirement` from the requirement status and due date. A proposal, the human decision, execution, and the measured outcome are separate records.

An authenticated member can approve, reject, or dismiss a proposal. Only an approved proposal can be executed. Execution records that the check ran and measures whether the intended outcome is achieved, not achieved, or unknown. Unknown means the facts needed for the judgment are missing. Execution does not change reminders, assignments, or engagement strategy.

### Planned, and not implemented

Predictive behavioral analysis is not implemented. The application does not identify personal work patterns, organizational bottlenecks, or personalized assistance from historical outcomes. It does not observe communications, score individuals, or change an engagement strategy. Those remain planned, and any future recommendation still requires explicit human approval before it changes how work is assigned or followed up.

## Agent 3 — Deliverable Verification

This agent is the evidence path for an approved requirement. It can receive a submission and store a file. It cannot decide that the file proves the requirement was met.

### Implemented

- Evidence metadata is attached to a requirement: description, type, submitter, timestamp, reference, and status.
- An authorized member can create a submission link. The public submission route accepts evidence for that link.
- Private object storage uses an S3-compatible endpoint. The application generates every key, writes one private bucket, and does not return an object URL. Storage credentials are separate from `SUPABASE_SERVICE_ROLE_KEY`. Source-document keys and evidence keys are different shapes, so evidence cleanup does not treat a source document as evidence.
- A member route reports evidence-file status and then refuses the download. It does not return object bytes or an object URL.

### Planned, and not implemented

AI evidence verification is not implemented. Nothing compares a submitted file with the requirement, classifies it as sufficient, or completes the requirement because a model accepted it. Recognition engines are not implemented. A person remains responsible for judging the evidence.

## Shared principles

These rules apply to every agent.

### Security and tenant isolation

Protected routes require a verified bearer JWT (`RS256` or `ES256`) and `X-Organization-ID`. The header selects a tenant. It grants nothing by itself. The JWT subject must have an active membership in that organization. An inaccessible workflow is `404`.

On PostgreSQL, application statements run as `commitmentops_app` with transaction-local `app.user_id` and `app.organization_id`. That role is not a superuser and does not bypass row-level security. Row-level security is enabled and forced, including on `source_documents`. Request handlers cannot turn it off. Organization and membership bootstrap stays on the migration connection.

People are shared records. They are not given an organization foreign key. A person insert on PostgreSQL is accepted only in a transaction that already references that person from a row the caller can write.

### Provenance and human approval

A requirement records how it was introduced: `organization_template`, `document_explicit`, `ai_inferred`, `user_provided`, or `system_derived`. AI-inferred requirements cannot start as authoritative. Document-explicit candidates are also non-authoritative until a person approves them. The model cannot approve work.

### Audit

A workflow save and its audit event commit in one transaction. API actor references are the verified JWT subject. Audit history is append-only through the repository. It records persistence activity. It is not a tamper-proof security log and it is not evidence of regulatory compliance.

Discovery decisions and learning signals are separate rows. A later save cannot rewrite them. Assistance proposals likewise keep the proposal, decision, execution, and outcome as separate facts.

### What stays outside the model

Deterministic business rules stay in application code. The model may interpret a document and recommend a candidate. It must not invent an owner, a date, or an evidence expectation to fill a gap, and it must not silently create an organizational requirement.

## Explicitly not implemented

The following are outside the current source tree. Do not describe them as present:

- Claude, or any provider other than the configured OpenAI-compatible chat endpoint
- RAG, embeddings, vector databases, fine-tuning, and model training
- Predictive behavioral analysis and personalized engagement
- AI evidence verification and document recognition
- Scheduled reports, email delivery, and multiple-approver policies
- Administrative membership management, role-based permissions, and token revocation

Weekly operational reports, monthly management reports, recipient lists, and configurable approval policies remain confirmed future requirements. They are not implied by the current models.
