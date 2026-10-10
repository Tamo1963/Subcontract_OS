"""Subcontract formation, flow-down matrix, release gate and amendments (roadmap Steps 7-8)."""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.models import (Amendment, ContractClause, DocumentRevision, FlowdownMapping, MainContract, ProjectSubcontractor,
                        Subcontract, Subcontractor, User, now)
from app.routers.common import get_or_404, load_subcontract, sub_out
from app.security import current_user, require
from app.services import flowdown, lifecycle

router = APIRouter(prefix="/api/v1", tags=["subcontracts & flow-down"])


class SubcontractIn(BaseModel):
    project_id: str
    main_contract_id: str | None = None
    subcontractor_id: str
    subcontract_number: str
    scope_summary: str
    discipline: str = "PIPING"
    currency: str = "INR"
    contract_value: str


@router.post("/subcontracts", status_code=201)
def create_subcontract(body: SubcontractIn, user: User = Depends(current_user), db: Session = Depends(get_db),
                       idempotency_key: str | None = Header(default=None)):
    require(db, user, "subcontract.draft", body.project_id)
    payload = body.model_dump()
    replay = audit.idempotent_lookup(db, idempotency_key, user.id, payload)
    if replay:
        return replay
    if not body.main_contract_id:   # CON-02
        raise HTTPException(422, {"code": "MAIN_CONTRACT_REQUIRED",
                                  "message": "A formal subcontract must reference its governing main contract"})
    mc = db.get(MainContract, body.main_contract_id)
    if mc is None or mc.project_id != body.project_id:   # CON-01 (also enforced by composite FK)
        raise HTTPException(422, {"code": "MAIN_CONTRACT_PROJECT_MISMATCH",
                                  "message": "Main contract does not belong to this project"})
    get_or_404(db, Subcontractor, body.subcontractor_id, "Subcontractor")
    elig = db.scalar(select(ProjectSubcontractor).where(ProjectSubcontractor.project_id == body.project_id,
                                                        ProjectSubcontractor.subcontractor_id == body.subcontractor_id))
    if not elig or elig.eligibility_status != "ELIGIBLE":
        raise HTTPException(422, {"code": "NOT_ELIGIBLE", "message": "Subcontractor is not eligible on this project"})
    try:
        value = Decimal(body.contract_value)
    except InvalidOperation:
        raise HTTPException(422, {"code": "INVALID_AMOUNT", "message": "contract_value must be a decimal string"})
    s = Subcontract(project_id=body.project_id, main_contract_id=body.main_contract_id,
                    subcontractor_id=body.subcontractor_id, subcontract_number=body.subcontract_number,
                    scope_summary=body.scope_summary, discipline=body.discipline.upper(), currency=body.currency,
                    contract_value=value, created_by=user.id)
    db.add(s)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {"code": "CONSTRAINT_VIOLATION", "message": "Subcontract number or reference invalid"})
    out = sub_out(s)
    audit.record(db, user.id, "subcontract.drafted", "Subcontract", s.id, project_id=s.project_id, after=out)
    audit.idempotent_store(db, idempotency_key, user.id, payload, out)
    db.commit()
    return out


@router.get("/subcontracts/{sid}")
def get_subcontract(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_subcontract(db, user, sid, "project.read")
    out = sub_out(s)
    out["release_gate"] = flowdown.release_gate(db, s)
    out["allowed_transitions"] = sorted(lifecycle.TRANSITIONS.get(s.status, []))
    out["subcontractor"] = db.get(Subcontractor, s.subcontractor_id).legal_name
    return out


class SubcontractPatch(BaseModel):
    scope_summary: str | None = None
    contract_value: str | None = None
    discipline: str | None = None


@router.patch("/subcontracts/{sid}")
def patch_subcontract(sid: str, body: SubcontractPatch, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    s = load_subcontract(db, user, sid, "subcontract.draft")
    if s.status in lifecycle.LOCKED_STATES:   # CON-03
        raise HTTPException(409, {"code": "BASELINE_LOCKED",
                                  "message": "Executed baseline is immutable; raise an amendment"})
    if s.status != "DRAFT":
        raise HTTPException(409, {"code": "NOT_EDITABLE", "message": "Return the subcontract to DRAFT to edit"})
    before = sub_out(s)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(s, k, Decimal(v) if k == "contract_value" else v)
    audit.record(db, user.id, "subcontract.edited", "Subcontract", s.id, project_id=s.project_id,
                 before=before, after=sub_out(s))
    db.commit()
    return sub_out(s)


class TransitionIn(BaseModel):
    target: str
    reason: str | None = None


@router.post("/subcontracts/{sid}/transitions")
def transition(sid: str, body: TransitionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_subcontract(db, user, sid, "subcontract.review")  \
        if body.target != "APPROVED_FOR_EXECUTION" else load_subcontract(db, user, sid, "subcontract.approve")
    lifecycle.transition(db, s, body.target, user, body.reason)
    return sub_out(s)


# ---------------------------------------------------------------- flow-down
def mapping_out(db: Session, m: FlowdownMapping) -> dict:
    c = db.get(ContractClause, m.source_clause_id)
    r = db.get(DocumentRevision, c.document_revision_id)
    return {"id": m.id, "clause_id": c.id, "source_document": r.document.document_number,
            "source_revision": r.revision_label, "clause_reference": c.clause_reference, "heading": c.heading,
            "clause_type": c.clause_type, "source_text": c.text, "source_passage_id": c.source_passage_id,
            "source_revision_id": c.document_revision_id, "risk_level": m.risk_level,
            "proposed_disposition": m.proposed_disposition, "proposed_rationale": m.proposed_rationale,
            "proposal_method": m.proposal_method, "disposition": m.disposition, "rationale": m.rationale,
            "subcontract_treatment": m.subcontract_treatment, "review_status": m.review_status,
            "reviewed_by": m.reviewed_by}


def _sort_key(m):
    return [int(x) for x in m["clause_reference"].split(".") if x.isdigit()]


@router.post("/subcontracts/{sid}/flowdown-mappings/generate")
def generate(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_subcontract(db, user, sid, "flowdown.decide")
    rows = flowdown.generate_matrix(db, s, user.id)
    return sorted([mapping_out(db, m) for m in rows], key=_sort_key)


@router.get("/subcontracts/{sid}/flowdown-mappings")
def list_mappings(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    load_subcontract(db, user, sid, "project.read")
    rows = db.scalars(select(FlowdownMapping).where(FlowdownMapping.subcontract_id == sid))
    return sorted([mapping_out(db, m) for m in rows], key=_sort_key)


class DecisionIn(BaseModel):
    disposition: str
    rationale: str
    subcontract_treatment: str | None = None


@router.post("/flowdown-mappings/{mid}/decision")
def decide(mid: str, body: DecisionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = get_or_404(db, FlowdownMapping, mid, "Mapping")
    load_subcontract(db, user, m.subcontract_id, "flowdown.decide")
    flowdown.decide(db, m, user.id, body.disposition, body.rationale, body.subcontract_treatment)
    return mapping_out(db, m)


@router.get("/subcontracts/{sid}/release-gate")
def gate(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_subcontract(db, user, sid, "project.read")
    return flowdown.release_gate(db, s)


class ExceptionIn(BaseModel):
    reason: str


@router.post("/subcontracts/{sid}/release-exception")
def release_exception(sid: str, body: ExceptionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_subcontract(db, user, sid, "flowdown.exception")
    if user.id == s.created_by:
        raise HTTPException(403, {"code": "SEGREGATION_OF_DUTIES", "message": "Drafter cannot accept own risk"})
    if len(body.reason.strip()) < 15:
        raise HTTPException(422, {"code": "REASON_REQUIRED", "message": "Record a substantive risk-acceptance reason"})
    gate_now = flowdown.release_gate(db, s)
    s.release_exception = {"accepted_by": user.id, "accepted_at": now().isoformat(), "reason": body.reason,
                           "covers_mapping_ids": gate_now["blocking_mapping_ids"]}
    audit.record(db, user.id, "flowdown.risk_exception_accepted", "Subcontract", s.id, project_id=s.project_id,
                 after=s.release_exception, reason=body.reason)
    db.commit()
    return flowdown.release_gate(db, s)


# ---------------------------------------------------------------- amendments (AUD-01)
class AmendmentIn(BaseModel):
    field: str
    new_value: str
    reason: str


@router.post("/subcontracts/{sid}/amendments", status_code=201)
def raise_amendment(sid: str, body: AmendmentIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_subcontract(db, user, sid, "subcontract.draft")
    if s.status not in lifecycle.LOCKED_STATES:
        raise HTTPException(409, {"code": "NOT_EXECUTED", "message": "Edit drafts directly; amendments apply to executed baselines"})
    if body.field not in ("contract_value", "scope_summary"):
        raise HTTPException(422, {"code": "FIELD_NOT_AMENDABLE", "message": "Amendable: contract_value, scope_summary"})
    n = (db.scalar(select(func.count()).select_from(Amendment).where(Amendment.subcontract_id == sid)) or 0) + 1
    a = Amendment(subcontract_id=sid, amendment_number=n, field=body.field, old_value=str(getattr(s, body.field)),
                  new_value=body.new_value, reason=body.reason, requested_by=user.id)
    db.add(a)
    db.flush()
    audit.record(db, user.id, "amendment.raised", "Amendment", a.id, project_id=s.project_id,
                 after={"field": a.field, "old": a.old_value, "new": a.new_value}, reason=a.reason)
    db.commit()
    return {"id": a.id, "amendment_number": n, "status": a.status}


@router.post("/amendments/{aid}/approve")
def approve_amendment(aid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = get_or_404(db, Amendment, aid, "Amendment")
    s = load_subcontract(db, user, a.subcontract_id, "amendment.approve")
    if a.status != "PENDING":
        raise HTTPException(409, {"code": "ALREADY_DECIDED", "message": a.status})
    if user.id == a.requested_by:
        raise HTTPException(403, {"code": "SEGREGATION_OF_DUTIES", "message": "Requester cannot approve own amendment"})
    before = {a.field: str(getattr(s, a.field))}
    setattr(s, a.field, Decimal(a.new_value) if a.field == "contract_value" else a.new_value)
    a.status, a.approved_by = "APPROVED", user.id
    audit.record(db, user.id, "amendment.approved", "Subcontract", s.id, project_id=s.project_id,
                 before=before, after={a.field: a.new_value, "amendment_id": a.id}, reason=a.reason)
    db.commit()   # baseline JSON is untouched: the original executed record is preserved
    return {"id": a.id, "status": a.status, "subcontract": sub_out(s)}


@router.get("/subcontracts/{sid}/amendments")
def list_amendments(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    load_subcontract(db, user, sid, "project.read")
    return [{"id": a.id, "number": a.amendment_number, "field": a.field, "old_value": a.old_value,
             "new_value": a.new_value, "reason": a.reason, "status": a.status}
            for a in db.scalars(select(Amendment).where(Amendment.subcontract_id == sid))]
