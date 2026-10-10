"""Subcontract lifecycle state machine, execution baseline and obligation generation (Sections 6.5, 8)."""
import re
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import (ContractClause, Document, ExtractionAssertion, FlowdownMapping, Obligation, Passage,
                        Subcontract, User, now)
from app.security import project_roles
from app.services.flowdown import APPLICABLE, release_gate
from app.services.ingestion import extract_assertions

TRANSITIONS = {
    "DRAFT": {"TECHNICAL_REVIEW", "WITHDRAWN"},
    "TECHNICAL_REVIEW": {"COMMERCIAL_REVIEW", "DRAFT"},
    "COMMERCIAL_REVIEW": {"LEGAL_REVIEW", "DRAFT"},
    "LEGAL_REVIEW": {"APPROVAL_PENDING", "DRAFT"},
    "APPROVAL_PENDING": {"APPROVED_FOR_EXECUTION", "DRAFT"},
    "APPROVED_FOR_EXECUTION": {"EXECUTED"},
    "EXECUTED": {"ACTIVE"},
    "ACTIVE": {"CLOSED_OUT"},
}
LOCKED_STATES = {"EXECUTED", "ACTIVE", "CLOSED_OUT"}

OBLIGATION_TEMPLATES = {   # clause type -> (trigger, evidence required, owner role)
    "NOTICE": ("Upon becoming aware of a delay or change event", "Dated notice and proof of receipt", "contracts_manager"),
    "TESTING": ("Before acceptance of the tested works", "Approved test procedure, calibration certificates, signed test report",
                "qaqc_manager"),
    "HSE": ("Within the stated period after the Commencement Date", "Approved HSE plan and induction records", "hse_manager"),
    "QUALITY": ("Before start of the relevant activity", "Approved ITP and inspection records", "qaqc_manager"),
    "INSURANCE": ("Within the stated period after the Commencement Date", "Insurance certificates", "commercial_manager"),
    "PAYMENT": ("Each payment cycle", "Measurement records and payment application", "commercial_manager"),
    "VARIATION": ("Upon receipt of an instruction", "Variation notice and valuation", "commercial_manager"),
    "WARRANTY": ("From taking-over", "Defects register and close-out records", "construction_manager"),
    "DELAY_LD": ("On milestone dates", "Programme and milestone achievement records", "project_controls"),
}
COMMENCEMENT_RE = re.compile(r"commencement date|date of (award|execution)|effective date", re.I)


def transition(db: Session, sub: Subcontract, target: str, user: User, reason: str | None = None) -> Subcontract:
    allowed = TRANSITIONS.get(sub.status, set())
    if target not in allowed:   # WF-01
        raise HTTPException(409, {"code": "INVALID_TRANSITION",
                                  "message": f"{sub.status} -> {target} is not permitted; allowed: {sorted(allowed)}"})
    if target == "APPROVED_FOR_EXECUTION":
        if "contracts_approver" not in (project_roles(db, user, sub.project_id) or set()):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "Only a contracts approver can approve release"})
        if user.id == sub.created_by:   # segregation of duties
            raise HTTPException(403, {"code": "SEGREGATION_OF_DUTIES", "message": "The drafter cannot approve release"})
        gate = release_gate(db, sub)
        if not gate["passed"]:   # CI-05
            raise HTTPException(409, {"code": "RELEASE_BLOCKED", "message": "Flow-down release gate failed", "gate": gate})
    if target == "EXECUTED":
        execute(db, sub, user)
    before = sub.status
    sub.status = target
    audit.record(db, user.id, "subcontract.transition", "Subcontract", sub.id, project_id=sub.project_id,
                 before={"status": before}, after={"status": target}, reason=reason)
    db.commit()
    return sub


def execute(db: Session, sub: Subcontract, user: User) -> None:
    """Freeze the executed baseline: files, hashes, revisions, approved deviations, value."""
    docs = list(db.scalars(select(Document).where(Document.subcontract_id == sub.id,
                                                  Document.document_type == "SUBCONTRACT",
                                                  Document.governing_revision_id.is_not(None))))
    if not docs:
        raise HTTPException(422, {"code": "NO_EXECUTED_DOCUMENT",
                                  "message": "Register and approve the signed subcontract document first"})
    revs = []
    for d in docs:
        r = next(r for r in d.revisions if r.id == d.governing_revision_id)
        revs.append({"document_number": d.document_number, "revision_id": r.id, "revision": r.revision_label,
                     "sha256": r.content_hash})
    maps = list(db.scalars(select(FlowdownMapping).where(FlowdownMapping.subcontract_id == sub.id)))
    sub.executed_at = now()
    sub.baseline = {"executed_at": sub.executed_at.isoformat(), "executed_by": user.id,
                    "contract_value": str(sub.contract_value), "currency": sub.currency,
                    "scope_summary": sub.scope_summary, "incorporated_documents": revs,
                    "flowdown": [{"mapping_id": m.id, "disposition": m.disposition} for m in maps],
                    "release_exception": sub.release_exception}
    generate_obligations(db, sub, user.id)


def add_days(start: date, n: int, basis: str) -> date:
    if basis == "CALENDAR":
        return start + timedelta(days=n)
    d, left = start, n
    while left:
        d += timedelta(days=1)
        if d.weekday() < 5:   # MVP: Mon-Fri; project calendars and holidays come next
            left -= 1
    return d


def generate_obligations(db: Session, sub: Subcontract, actor_id: str) -> list[Obligation]:
    maps = list(db.scalars(select(FlowdownMapping).where(FlowdownMapping.subcontract_id == sub.id,
                                                         FlowdownMapping.disposition.in_(APPLICABLE))))
    have = {o.source_mapping_id for o in db.scalars(select(Obligation).where(Obligation.subcontract_id == sub.id))}
    count = db.scalar(select(func.count()).select_from(Obligation).where(Obligation.subcontract_id == sub.id)) or 0
    created = []
    for m in maps:
        clause = db.get(ContractClause, m.source_clause_id)
        if m.id in have or clause.clause_type not in OBLIGATION_TEMPLATES:
            continue
        trig, evid, owner = OBLIGATION_TEMPLATES[clause.clause_type]
        # The obligation is created by the SUBCONTRACT treatment where one was recorded; otherwise the
        # source clause applies as flowed down. Reviewed assertion values take precedence over raw ones.
        days, basis = (None, "CALENDAR")
        treated = [x for x in extract_assertions(m.subcontract_treatment or "") if x["field_name"] == "NOTICE_PERIOD_DAYS"]
        if treated:
            d, basis = treated[0]["normalised_value"].split("|")
            days = int(d)
        else:
            a = db.scalar(select(ExtractionAssertion).join(Passage).where(
                Passage.id == clause.source_passage_id, ExtractionAssertion.field_name == "NOTICE_PERIOD_DAYS",
                ExtractionAssertion.validation_status != "REJECTED"))
            if a:
                d, basis = a.normalised_value.split("|")
                days = int(d)
        due = None
        if days is not None and COMMENCEMENT_RE.search((m.subcontract_treatment or "") + " " + clause.text) and sub.executed_at:
            due = add_days(sub.executed_at.date(), days, basis).isoformat()
        first = re.split(r"(?<=\.)\s", clause.text.strip(), maxsplit=1)[0]
        count += 1
        o = Obligation(obligation_number=f"OBL-{count:03d}", subcontract_id=sub.id, source_mapping_id=m.id,
                       obligation_type=clause.clause_type,
                       statement=(f"[Subcontract treatment] {m.subcontract_treatment}" if m.subcontract_treatment
                                  else f"[Summary] {clause.heading}: {first}"),
                       source_wording=clause.text,
                       trigger=trig, notice_period_days=days, day_basis=basis, due_at=due,
                       evidence_required=evid, owner_role=owner, risk_level=m.risk_level)
        db.add(o)
        db.flush()
        audit.record(db, actor_id, "obligation.created", "Obligation", o.id, project_id=sub.project_id,
                     after={"number": o.obligation_number, "source_mapping_id": m.id, "due_at": due})
        created.append(o)
    return created
