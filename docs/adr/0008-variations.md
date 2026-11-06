# ADR-0008 — Variations (Phase 4, first module)

**Status:** Accepted · 6 Nov 2026 · Payments remain deferred (approved variations are not yet paid through the OS)

## Context
Blueprint §5.3 requires a variation workflow with separate notice, entitlement, valuation and approval
stages, and acceptance tests VAR-01 (no silent waiver of a required notice) and VAR-02 (approved value
changes the forecast only by defined rules).

## Decision
1. **Project events** (`execution_project_events`) record dated facts (instruction, design change, access
   delay, site condition…). An event never asserts entitlement; it MAY_GIVE_RISE_TO a variation.
2. **Workflow** (`commercial_variations`):
   `IDENTIFIED → CLASSIFIED → NOTICE_REVIEW → ENTITLEMENT_ASSESSMENT → COST_AND_TIME_ASSESSMENT →
   COMMERCIAL_REVIEW → APPROVAL_PENDING → APPROVED / REJECTED / WITHDRAWN → INCORPORATED → CLOSED`,
   with step-backs for rework. Each entry has a gate (classification, notice, entitlement, valuation).
   Variations are raised only on executed/active subcontracts.
3. **Notice (VAR-01).** The period comes from the subcontract's own flowed-down obligation (variation notice
   if present, else the general notice), with its day basis, from the event date. Statuses: pending, given
   on time, given late, given without document, missing/overdue, no clause, not required. A notice is
   "given" only with the notice document attached. Anything but on-time blocks entitlement assessment until
   a **legal reviewer or contracts approver** records *proceed despite notice risk* or *notice not required*
   with a reason. The underlying fact (e.g. "given late") stays visible; the decision waives no right.
4. **Entitlement** must cite subcontract clauses (warned if a clause is not in this subcontract's flow-down).
   Not entitled → reject (reason required).
5. **Valuation.** Priced lines (cost type, pricing basis, quantity, unit, rate, rate reference); the server
   computes every amount in fixed-point decimals; star rates and quotations need a reference; negative
   quantities only for scope omissions. Claimed, assessed, recommended and approved amounts and days are
   separate fields — none overwrites another.
6. **Segregation of duties.** The assessor cannot recommend (commercial manager); whoever raised, assessed
   or recommended cannot decide (contracts approver). Approving a value other than the recommendation, a
   rejection or a return needs a reason. Decision and raise accept idempotency keys.
7. **Value rules (VAR-02).** Only *incorporation* changes the subcontract value — through an approved
   amendment carrying the approver; the executed baseline is never edited.
   Forecast = current value + approved-not-incorporated. Open variations are shown at their latest
   estimate (recommended → assessed → claimed) **outside** the forecast, with additions and omissions
   reported separately (never netted).
8. **Trace:** event → variation → notice obligation → main-contract source clause → evidence → approval →
   amendment. Every step is in the audit trail.

## Consequences
- New demo user `kabir.commercial` (commercial manager on P-101) so the demo can show segregation of duties.
- Not yet included: payment certification of approved variations (payments module), programme (time-impact)
  analysis, subcontractor-portal submission, configurable approval limits per value band.

## Tests
`tests/test_variations.py` (12): notice clock source, missing/late/pending notice, no silent waiver, value
rules, omissions not netted, server-side valuation, segregation of duties, state machine, access, idempotency,
events and trace.
