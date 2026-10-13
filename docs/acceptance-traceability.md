# Acceptance-test traceability (blueprint Section 14 → `tests/test_acceptance.py`)

| Blueprint ID | Requirement | Test | Status |
|---|---|---|---|
| REG-01 | Duplicate legal entity detected | `test_REG01_duplicate_legal_entity_detected` | ✅ |
| REG-02 | One identity, separate project relationships | `test_REG02_one_identity_two_projects_separate_visibility` | ✅ |
| CON-01 | Cross-project main contract rejected (API + DB composite FK) | `test_CON01_*` (2) | ✅ |
| CON-02 | Formal subcontract needs a main contract | `test_CON02_*` | ✅ |
| CON-03 | Executed baseline immutable | `test_CON03_*` | ✅ |
| CON-04 | Superseded revision retained and retrievable | `test_CON04_*` | ✅ |
| DOC-01 | Same file twice → duplicate, no new records | `test_DOC01_*` | ✅ |
| DOC-02 | Scans/PDF locators (MVP: page locators for text PDFs; OCR is next) | `test_DOC02_*` | ✅ partial |
| DOC-03 | BOQ cell provenance and formulas | `test_DOC03_*` | ✅ |
| DOC-04 | Conflicting revision label not guessed | `test_DOC04_*` | ✅ |
| DOC-05 | Unsafe / malformed files quarantined | `test_DOC05_*` | ✅ |
| DOC-06 | Interrupted job retries without duplicates | `test_DOC06_*` | ✅ |
| DOC-07 | New parser version versioned, approved records kept | `test_DOC07_*` | ✅ |
| CI-01 | Obligation links to exact revision and passage | `test_CI01_*` | ✅ |
| CI-02 | Governing version used; history on request | `test_CI02_*` | ✅ |
| CI-03 | Conflicts surfaced with both sources | `test_CI03_*` | ✅ |
| CI-04 | Every in-scope clause has a disposition row | `test_CI04_*` | ✅ |
| CI-05 | Release blocked; exception only by a separate approver | `test_CI05_*` | ✅ |
| CI-06 | Insufficient evidence reported | `test_CI06_*` | ✅ |
| CI-07 | Revoked access denies search and download | `test_CI07_*` | ✅ |
| CI-08 | Embedded instructions treated as data | `test_CI08_*` | ✅ |
| SEC-01 | Cross-project retrieval denied | `test_SEC01_*` | ✅ |
| — | Classification above clearance hidden | `test_SEC02_*` | ✅ |
| WF-01 | Invalid lifecycle transition rejected | `test_WF01_*` | ✅ |
| PAY-03 (pattern) | Segregation of duties (release approval, risk exception, evidence, amendments) | `test_SOD01_*`, `test_CI05_*`, `test_VS01_*` | ✅ |
| PAY-02 (pattern) | Idempotency keys; same key + different payload rejected | `test_IDEM01_*`, `test_IDEM02_*` | ✅ |
| AUD-01 | Amendment retains actor, time, reason, old/new | `test_AUD01_*` | ✅ |
| Vertical slice | Source → flow-down → approval → obligation → evidence (reject, then accept) → trace | `test_VS01_*` | ✅ |
| AI-01 | Provider chosen from dropdown catalogue; any endpoint | `test_catalogue_*`, `test_each_protocol_*` (6) | ✅ |
| AI-02 | Keys write-only and encrypted; admin-only configuration | `test_only_admin_registers_*`, `test_endpoint_validation` | ✅ |
| AI-03 | Model output is a proposal; invalid output or outage falls back to rules | `test_flowdown_uses_assigned_model_*`, `test_invalid_model_output_*`, `test_unreachable_provider_*` | ✅ |
| AI-04 | RESTRICTED content never sent to CLOUD providers | `test_restricted_content_never_sent_*` | ✅ |
| AI-05 | Model-drafted answers only cite retrieved sources | `test_knowledge_draft_is_citation_checked` | ✅ |
| AI-06 | Call log holds metadata, not content | `test_call_log_holds_metadata_not_content` | ✅ |
| SEC-03 | Temporary password must be changed before any other call | `test_temporary_password_forces_change_*` | ✅ |
| SEC-04 | Salted password hashes; generic sign-in errors; lockout and unlock | `test_password_stored_as_salted_hash`, `test_generic_error_and_lockout` | ✅ |
| SEC-05 | Sessions expire and are revoked on sign-out / reset; CSRF header on cookie writes | `test_logout_*`, `test_session_expiry`, `test_reset_password_*`, `test_csrf_*` | ✅ |
| SEC-06 | Admin-only user administration; one-time temporary passwords; dev header off by default | `test_admin_creates_user_*`, `test_dev_header_rejected_*`, `test_admin_cannot_lock_*` | ✅ |
| SSO-01 | Any OIDC provider added by configuration; secret never returned; HTTPS issuer | `test_provider_admin_*`, `test_issuer_validation`, `test_connection_test` | ✅ |
| SSO-02 | Invited user linked once by verified e-mail; then by permanent subject; roles stay in OS | `test_invited_user_*`, `test_session_from_sso_*`, `test_unverified_email_*` | ✅ |
| SSO-03 | Invite-only default; optional auto-create limited to allowed domains, no roles | `test_invite_only_*`, `test_auto_provision_*` | ✅ |
| SSO-04 | State one-time and browser-bound; tampered or forged ID tokens rejected (7 cases); wrong client secret fails | `test_state_*`, `test_tampered_id_tokens_*`, `test_wrong_client_secret_*` | ✅ |
| SSO-05 | Self-service linking (CSRF-protected); one identity per account; disabled account/provider refused | `test_signed_in_user_links_*`, `test_one_identity_*`, `test_disabled_*` | ✅ |
| SSO-06 | Password sign-in limitable to admins (break-glass kept); last sign-in method cannot be unlinked; events audited | `test_password_login_*`, `test_cannot_unlink_*`, `test_sso_events_*` | ✅ |
| DB-01 | Whole suite passes on PostgreSQL 16; migrations match models (`alembic check`) | CI job `test-postgres` | ✅ |
| WF-02/03, VAR-01/02, PAY-01 | Mobilisation, variations, payments | — | Phase 4–5 (not in MVP) |
| REC-01 | Restore drill | — | Requires the pilot environment |
