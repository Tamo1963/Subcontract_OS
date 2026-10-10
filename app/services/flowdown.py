"""GCC flow-down engine and release gate (blueprint Section 7).

The proposer is the AI seam: ``RulesProposer`` is a deterministic stand-in behind the same
interface a model-backed proposer will implement. Proposals never become decisions on their own;
a reviewer records the disposition, and unresolved HIGH-risk provisions block release.
"""
import re
from typing import Protocol

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import ContractClause, Document, FlowdownMapping, Subcontract, now
from app.services.ingestion import clause_risk

DISPOSITIONS = {"DIRECT", "ADAPTED", "PROJECT_SPECIFIC", "COMMERCIAL_DEVIATION", "NOT_APPLICABLE", "UNRESOLVED"}
APPLICABLE = {"DIRECT", "ADAPTED", "PROJECT_SPECIFIC", "COMMERCIAL_DEVIATION"}

DISCIPLINE_TERMS = {
    "PIPING": r"piping|pipe|weld|isometric|spool",
    "CIVIL": r"civil|concrete|foundation|earthwork|rebar",
    "ELECTRICAL": r"electrical|cable|switchgear|transformer",
    "MECHANICAL": r"equipment erection|rotating|mechanical|alignment",
    "INSTRUMENTATION": r"instrument|loop check|calibration of transmitters",
}


class FlowdownProposer(Protocol):
    name: str

    def propose(self, clause: ContractClause, sub: Subcontract) -> tuple[str, str]: ...


class RulesProposer:
    name = "rules-v1"

    def propose(self, clause: ContractClause, sub: Subcontract) -> tuple[str, str]:
        text = f"{clause.heading} {clause.text}".lower()
        mentioned = [d for d, pat in DISCIPLINE_TERMS.items() if re.search(pat, text)]
        if mentioned and sub.discipline not in mentioned and clause.clause_type in ("TESTING", "QUALITY"):
            return "NOT_APPLICABLE", (f"Clause addresses {', '.join(mentioned).lower()} scope; subcontract "
                                      f"discipline is {sub.discipline.lower()}. Confirm exclusion.")
        k = clause.clause_type
        if k in ("DEFINITIONS", "GENERAL", "CONFIDENTIALITY", "HSE", "QUALITY", "TESTING"):
            return "DIRECT", "Applies substantially as written to the subcontract scope; adjust party references."
        if k in ("NOTICE", "VARIATION"):
            return "ADAPTED", ("Flow down with adapted party references and a shorter notice period, so the "
                               "Contractor can meet its own notice obligation under the main contract.")
        if k == "PAYMENT":
            return "ADAPTED", "Adapt certification cycle, retention and set-off to the subcontract payment terms."
        if k == "WARRANTY":
            return "ADAPTED", "Align defects period so it covers the main-contract defects liability period."
        if k == "DELAY_LD":
            return "COMMERCIAL_DEVIATION", ("LD pass-through needs commercial evaluation (rate, cap, milestone "
                                            "mapping). Do not impose automatically.")
        if k in ("INSURANCE", "INDEMNITY_LIABILITY"):
            return "PROJECT_SPECIFIC", "Requires a tailored special condition sized to the subcontract value and risk."
        if k in ("TERMINATION", "DISPUTE"):
            return "UNRESOLVED", "Requires an authorised legal decision before release."
        return "UNRESOLVED", "No rule matched; requires review."


PROPOSER: FlowdownProposer = RulesProposer()


def source_clauses(db: Session, sub: Subcontract) -> list[ContractClause]:
    """Clauses from the GOVERNING revisions of the main-contract documents only."""
    docs = db.scalars(select(Document).where(Document.main_contract_id == sub.main_contract_id,
                                             Document.governing_revision_id.is_not(None)))
    rev_ids = [d.governing_revision_id for d in docs]
    if not rev_ids:
        return []
    return list(db.scalars(select(ContractClause).where(ContractClause.document_revision_id.in_(rev_ids))))


def generate_matrix(db: Session, sub: Subcontract, actor_id: str) -> list[FlowdownMapping]:
    if sub.status not in ("DRAFT", "TECHNICAL_REVIEW", "COMMERCIAL_REVIEW", "LEGAL_REVIEW"):
        raise HTTPException(409, {"code": "BASELINE_LOCKED", "message": "Flow-down can only change before approval"})
    clauses = source_clauses(db, sub)
    if not clauses:
        raise HTTPException(422, {"code": "NO_SOURCE_CLAUSES",
                                  "message": "No governing main-contract revision with extracted clauses"})
    existing = {m.source_clause_id for m in db.scalars(select(FlowdownMapping).where(
        FlowdownMapping.subcontract_id == sub.id))}
    created = 0
    for c in clauses:   # every in-scope clause gets a row (CI-04); re-running is idempotent
        if c.id in existing:
            continue
        disp, why = PROPOSER.propose(c, sub)
        db.add(FlowdownMapping(subcontract_id=sub.id, main_contract_id=sub.main_contract_id, source_clause_id=c.id,
                               proposed_disposition=disp, proposed_rationale=why, proposal_method=PROPOSER.name,
                               risk_level=clause_risk(c.clause_type)))
        created += 1
    audit.record(db, actor_id, "flowdown.generated", "Subcontract", sub.id, project_id=sub.project_id,
                 after={"new_rows": created, "total_clauses": len(clauses)})
    db.commit()
    return list(db.scalars(select(FlowdownMapping).where(FlowdownMapping.subcontract_id == sub.id)))


def decide(db: Session, m: FlowdownMapping, actor_id: str, disposition: str, rationale: str,
           treatment: str | None) -> FlowdownMapping:
    if disposition not in DISPOSITIONS:
        raise HTTPException(422, {"code": "INVALID_DISPOSITION", "message": f"Use one of {sorted(DISPOSITIONS)}"})
    if not rationale.strip():
        raise HTTPException(422, {"code": "RATIONALE_REQUIRED", "message": "A rationale is required"})
    sub = db.get(Subcontract, m.subcontract_id)
    if sub.status not in ("DRAFT", "TECHNICAL_REVIEW", "COMMERCIAL_REVIEW", "LEGAL_REVIEW", "APPROVAL_PENDING"):
        raise HTTPException(409, {"code": "BASELINE_LOCKED", "message": "Executed baseline cannot change"})
    before = audit.snapshot(m, ["disposition", "rationale", "review_status"])
    m.disposition, m.rationale, m.subcontract_treatment = disposition, rationale, treatment
    m.review_status = "APPROVED" if disposition != "UNRESOLVED" else "UNRESOLVED"
    m.reviewed_by, m.reviewed_at = actor_id, now()
    audit.record(db, actor_id, "flowdown.decided", "FlowdownMapping", m.id, project_id=sub.project_id,
                 before=before, after=audit.snapshot(m, ["disposition", "rationale", "review_status"]))
    db.commit()
    return m


def release_gate(db: Session, sub: Subcontract) -> dict:
    rows = list(db.scalars(select(FlowdownMapping).where(FlowdownMapping.subcontract_id == sub.id)))
    blocking, warnings = [], []
    for m in rows:
        unresolved = m.review_status != "APPROVED"
        if unresolved and m.risk_level == "HIGH":
            blocking.append(m.id)
        elif unresolved:
            warnings.append(m.id)
    total = len(source_clauses(db, sub))
    missing = total - len(rows)
    exception = sub.release_exception
    passed = (not blocking or bool(exception)) and missing == 0 and total > 0
    return {"passed": passed, "blocking_mapping_ids": blocking, "warning_mapping_ids": warnings,
            "clauses_without_mapping": missing, "mapped": len(rows), "exception": exception}
