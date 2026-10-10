# ADR-0001 — MVP architecture baseline

Status: **Accepted for MVP, pending confirmation in the Step 2 architecture review** · 2026-10-10

The five decisions in blueprint Section 16 were not selected, so the MVP was built on these assumed defaults.

| # | Decision | Assumed default used in the MVP | How the code keeps the option open |
|---|---|---|---|
| 1 | Deployment scale | One pilot project, architecture ready to scale | Multi-project data model and permission checks from day one (a second demo project, P-202, exists to prove isolation) |
| 2 | Private-environment contents | Source documents and extraction private; approved metadata may run in managed hosting | Object store and DB are configured by `SCOS_DATA_DIR` / `SCOS_DATABASE_URL`; no external AI service is called, so nothing leaves the deployment |
| 3 | Existing data to migrate | Files plus Excel registers, BOQs, payment sheets | Spreadsheet parser keeps cell addresses and formulas (DOC-03) |
| 4 | First vertical slice | Document ingestion → flow-down → obligation register | Implemented end-to-end (test VS-01) |
| 5 | Deployment model | Modular monolith with asynchronous workers | Domain routers/services are separate modules; processing jobs are idempotent and resumable, ready to move to a worker queue |

## Consequences
- SQLite is used for the MVP; the model maps 1:1 to PostgreSQL schemas (`registry_*` → `registry.*`). Move to PostgreSQL + Alembic migrations before pilot data.
- Authentication is a development stub (`X-User` header). It must be replaced by the enterprise IdP with MFA before any real use. Authorisation itself (roles, project scope, classification, segregation of duties) is real and tested.
- The AI seam is `FlowdownProposer` (rules-based `rules-v1` today) and the lexical retrieval stage. A model-backed proposer and a pgvector stage plug in behind the same interfaces; outputs stay proposals.
