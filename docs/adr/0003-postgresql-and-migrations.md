# ADR-0003 — PostgreSQL as the system of record, with versioned migrations

Status: **Accepted** · 2026-10-11

## Decision
PostgreSQL 16 is the authoritative database. SQLite remains only for quick local development and the
fast test run. Schema changes are versioned with Alembic; production never relies on `create_all`.

## Details
| Item | Choice |
|---|---|
| Driver / URL | `psycopg` 3 · `SCOS_DATABASE_URL=postgresql+psycopg://scos_app:<password>@host:5432/subcontract_os` (from `.env`, never committed) |
| Application role | `scos_app`, owns only the `subcontract_os` database; `PUBLIC` connect and create rights revoked |
| Temporary credentials | `db/setup_postgres.sql` creates the role with a random password and `VALID UNTIL now() + 30 days`. Rotate with `ALTER ROLE scos_app PASSWORD '…' VALID UNTIL 'infinity'` (or a managed secret) before the pilot. |
| Setup | Windows `scripts/setup_postgres.ps1`; Linux/macOS `scripts/setup_postgres.sh`; containers `docker-compose.yml` |
| Migrations | `migrations/versions/0001_baseline_schema.py` (all tables, composite FK `fk_subcontract_main_contract_same_project`). Applied automatically at start-up (`SCOS_AUTO_MIGRATE=1`) or with `python -m app.cli migrate`. CI runs `alembic check` to prove models and migrations agree. |
| Tests | Same 62-test suite runs on SQLite and on PostgreSQL (`SCOS_TEST_DATABASE_URL`). |
| Logical schemas | Tables keep their domain prefix (`registry_`, `contracts_`, …) in `public`. Moving them into separate PostgreSQL schemas is a later, mechanical migration if the DBA prefers it. |

## Still to do before live data
Backups and a restore drill (REC-01), TLS to the database, a managed secret for the DB password, and
connection limits sized to the pilot load.
