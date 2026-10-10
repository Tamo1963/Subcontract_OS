"""Obligation register, deadlines, evidence closure and traceability (roadmap Step 9, vertical slice)."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.models import (ContractClause, Document, DocumentRevision, EvidenceLink, FlowdownMapping, Obligation,
                        Passage, Subcontract, User)
from app.routers.common import get_or_404, load_subcontract
from app.security import current_user, require
from app.services.lifecycle import add_days, generate_obligations

router = APIRouter(prefix="/api/v1", tags=["obligations & evidence"])


def ob_out(o: Obligation, today: date | None = None) -> dict:
    today = today or date.today()
    overdue = bool(o.due_at and o.status not in ("CLOSED",) and date.fromisoformat(o.due_at) < today)
    return {"id": o.id, "number": o.obligation_number, "subcontract_id": o.subcontract_id,
            "origin_kind": o.origin_kind, "source_mapping_id": o.source_mapping_id,
            "internal_control_origin": o.internal_control_origin, "type": o.obligation_type,
            "statement": o.statement, "source_wording": o.source_wording, "responsible_party": o.responsible_party,
            "owner_role": o.owner_role, "trigger": o.trigger, "notice_period_days": o.notice_period_days,
            "day_basis": o.day_basis, "timezone": o.timezone, "due_at": o.due_at,
            "evidence_required": o.evidence_required, "risk_level": o.risk_level, "status": o.status,
            "overdue": overdue}


def _load_ob(db, user, oid, action="project.read") -> tuple[Obligation, Subcontract]:
    o = get_or_404(db, Obligation, oid, "Obligation")
    s = load_subcontract(db, user, o.subcontract_id, action)
    return o, s


@router.get("/subcontracts/{sid}/obligations")
def list_obligations(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    load_subcontract(db, user, sid, "project.read")
    return [ob_out(o) for o in db.scalars(select(Obligation).where(Obligation.subcontract_id == sid)
                                          .order_by(Obligation.obligation_number))]


@router.post("/subcontracts/{sid}/obligations/generate")
def regenerate(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_subcontract(db, user, sid, "obligation.manage")
    if s.status not in ("EXECUTED", "ACTIVE"):
        raise HTTPException(409, {"code": "NOT_EXECUTED", "message": "Obligations derive from the executed baseline"})
    created = generate_obligations(db, s, user.id)   # idempotent: one obligation per mapping
    db.commit()
    return {"created": len(created)}


class InternalControlIn(BaseModel):
    statement: str
    internal_control_origin: str
    owner_role: str = "contracts_manager"
    due_at: str | None = None


@router.post("/subcontracts/{sid}/obligations", status_code=201)
def add_internal_control(sid: str, body: InternalControlIn, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    """Internal controls are tracked but never presented as contractual duties."""
    s = load_subcontract(db, user, sid, "obligation.manage")
    if not body.internal_control_origin.strip():
        raise HTTPException(422, {"code": "ORIGIN_REQUIRED", "message": "Document the internal-control origin"})
    n = (db.scalar(select(func.count()).select_from(Obligation).where(Obligation.subcontract_id == sid)) or 0) + 1
    o = Obligation(obligation_number=f"IC-{n:03d}", subcontract_id=sid, origin_kind="INTERNAL_CONTROL",
                   internal_control_origin=body.internal_control_origin, obligation_type="INTERNAL_CONTROL",
                   statement=body.statement, source_wording="(internal control - no contractual source)",
                   responsible_party="CONTRACTOR", owner_role=body.owner_role, due_at=body.due_at, risk_level="LOW")
    db.add(o)
    db.flush()
    audit.record(db, user.id, "obligation.internal_control_created", "Obligation", o.id, project_id=s.project_id,
                 after=ob_out(o))
    db.commit()
    return ob_out(o)


class TriggerIn(BaseModel):
    occurred_on: str
    note: str = ""


@router.post("/obligations/{oid}/trigger")
def trigger(oid: str, body: TriggerIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o, s = _load_ob(db, user, oid, "obligation.manage")
    if o.notice_period_days is None:
        raise HTTPException(422, {"code": "NO_PERIOD", "message": "No time period recorded for this obligation"})
    before = {"due_at": o.due_at}
    o.due_at = add_days(date.fromisoformat(body.occurred_on), o.notice_period_days, o.day_basis).isoformat()
    audit.record(db, user.id, "obligation.triggered", "Obligation", o.id, project_id=s.project_id, before=before,
                 after={"due_at": o.due_at, "trigger_date": body.occurred_on}, reason=body.note)
    db.commit()
    return ob_out(o)


class EvidenceIn(BaseModel):
    document_revision_id: str
    note: str = ""


@router.post("/obligations/{oid}/evidence", status_code=201)
def submit_evidence(oid: str, body: EvidenceIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o, s = _load_ob(db, user, oid, "evidence.submit")
    r = get_or_404(db, DocumentRevision, body.document_revision_id, "Revision")
    if r.document.project_id != s.project_id:
        raise HTTPException(422, {"code": "CONTEXT_MISMATCH", "message": "Evidence must belong to the same project"})
    e = EvidenceLink(obligation_id=o.id, document_revision_id=r.id, submitted_by=user.id, note=body.note)
    db.add(e)
    o.status = "EVIDENCE_SUBMITTED"
    db.flush()
    audit.record(db, user.id, "evidence.submitted", "Obligation", o.id, project_id=s.project_id,
                 after={"evidence_id": e.id, "revision_id": r.id, "sha256": r.content_hash})
    db.commit()
    return {"id": e.id, "obligation": ob_out(o)}


class EvidenceReviewIn(BaseModel):
    decision: str   # ACCEPT | REJECT
    comment: str


@router.post("/evidence/{eid}/review")
def review_evidence(eid: str, body: EvidenceReviewIn, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    e = get_or_404(db, EvidenceLink, eid, "Evidence")
    o, s = _load_ob(db, user, e.obligation_id, "evidence.review")
    if user.id == e.submitted_by:
        raise HTTPException(403, {"code": "SEGREGATION_OF_DUTIES", "message": "Submitter cannot accept own evidence"})
    if body.decision not in ("ACCEPT", "REJECT") or not body.comment.strip():
        raise HTTPException(422, {"code": "INVALID_REVIEW", "message": "ACCEPT/REJECT with a comment"})
    e.review_status = "ACCEPTED" if body.decision == "ACCEPT" else "REJECTED"
    e.reviewed_by, e.review_comment = user.id, body.comment
    before = o.status
    # Obligation stays open unless acceptance criteria are met.
    o.status = "CLOSED" if body.decision == "ACCEPT" else "OPEN"
    audit.record(db, user.id, f"evidence.{e.review_status.lower()}", "Obligation", o.id, project_id=s.project_id,
                 before={"status": before}, after={"status": o.status, "evidence_id": e.id}, reason=body.comment)
    db.commit()
    return ob_out(o)


@router.get("/obligations/{oid}/trace")
def trace(oid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Obligation -> flow-down decision -> source clause -> exact passage -> governing revision -> evidence."""
    o, s = _load_ob(db, user, oid)
    chain = {"obligation": ob_out(o), "subcontract": {"id": s.id, "number": s.subcontract_number,
                                                     "status": s.status, "baseline_executed_at": s.executed_at.isoformat() if s.executed_at else None}}
    if o.source_mapping_id:
        m = db.get(FlowdownMapping, o.source_mapping_id)
        c = db.get(ContractClause, m.source_clause_id)
        p = db.get(Passage, c.source_passage_id)
        r = db.get(DocumentRevision, c.document_revision_id)
        d = db.get(Document, r.document_id)
        chain["flowdown"] = {"id": m.id, "disposition": m.disposition, "rationale": m.rationale,
                             "treatment": m.subcontract_treatment, "reviewed_by": m.reviewed_by,
                             "proposal": m.proposed_disposition, "proposal_method": m.proposal_method}
        chain["source_clause"] = {"id": c.id, "reference": c.clause_reference, "heading": c.heading,
                                  "type": c.clause_type, "text": c.text}
        chain["source_passage"] = {"id": p.id, "page": p.page_number, "section": p.section_reference, "text": p.text}
        chain["source_revision"] = {"id": r.id, "document_number": d.document_number, "title": d.title,
                                    "revision": r.revision_label, "sha256": r.content_hash,
                                    "governing": d.governing_revision_id == r.id, "status": r.business_status}
    chain["evidence"] = []
    for e in db.scalars(select(EvidenceLink).where(EvidenceLink.obligation_id == o.id)):
        r = db.get(DocumentRevision, e.document_revision_id)
        chain["evidence"].append({"id": e.id, "document_number": r.document.document_number, "revision": r.revision_label,
                                  "sha256": r.content_hash, "submitted_by": e.submitted_by, "note": e.note,
                                  "review_status": e.review_status, "reviewed_by": e.reviewed_by,
                                  "review_comment": e.review_comment})
    return chain
