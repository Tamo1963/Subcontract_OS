# ADR-0009 — Mobilisation checklist and site enablers (Phase 4)

**Status:** Accepted · 7 Nov 2026 · Payments remain deferred

## Context
Blueprint §5.2 (mobilisation workflow: exit evidence is a *verified* mobilisation checklist), §6.1 (project
enablers need their own dependency register) and acceptance test WF-02 (closure without evidence is blocked or
routed through a specifically authorised exception).

## Decision
1. **Checklist generated at execution** from a standard template (security, insurances, HSE plan, quality plan,
   key personnel, programme, statutory registrations, equipment, induction, work front, utilities, laydown,
   welfare) plus a **discipline pack** (piping, mechanical, civil, electrical). Templates are configuration in
   `app/services/mobilisation.py`, not sector logic in the core; items can be added by hand. Generation is
   idempotent.
2. **Item lifecycle:** OPEN → SUBMITTED (evidence document required) → VERIFIED by a person holding one of the
   item's verifier roles who did not submit it; *Return* reopens it with a reason. **Exception** (WAIVED) only by a
   contracts approver with a recorded reason and optional follow-up date (WF-02).
3. **Start-of-work gate:** EXECUTED → ACTIVE is refused (`MOBILISATION_INCOMPLETE`, listing the items) while any
   mandatory item is open.
4. **Enablers** (`execution_enablers`): type, location, capacity, responsible party, cost allocation, owner,
   planned / forecast / actual dates, prerequisites. Readiness is **claimed with evidence** (not for a future
   date) and **verified by another person**; verification needs prerequisites ready. Planned dates change only
   with a reason; dates are fixed once verified.
5. **Need-by links** connect an enabler to each subcontract that depends on it. Lateness = available
   (actual, else forecast/planned and never earlier than today) − need-by: ON_TRACK / AT_RISK before delivery,
   ON_TIME / LATE after. A site checklist item linked to an enabler closes only when the enabler is READY, using its
   readiness evidence.
6. **Lateness is a fact, not entitlement.** *Record delay event* turns measured lateness into a dated project event
   (ACCESS_DELAY for work fronts/roads, FREE_ISSUE_DELAY otherwise) linked to the enabler; a variation can be raised
   from it, and the variation trace reaches the enabler (LATENESS_RECORDED_IN).

## Consequences
- New demo user `rohan.site` (construction manager) owns the enablers; `priya.coord` verifies them.
- New demo subcontract SC-101-03 (Konkan Electricals, E&I) is executed and mobilising — blocked by the late
  substation handover EN-006. VAR-002 on SC-101-01 now traces to the late PR-3 work front EN-004.
- Not yet included: subcontractor self-submission (portal), resource histograms against actuals, programme links.

## Tests
`tests/test_mobilisation.py` (9): template and discipline pack, WF-02 exception rules, second-person verification,
start-work gate, enabler-linked item, readiness rules and prerequisites, lateness, delay event to variation trace,
project-scoped access.
