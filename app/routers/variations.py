"""Project events and variations (blueprint Section 5.3, acceptance VAR-01/02, ADR-0008)."""
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.models import (Amendment, AuditEvent, ContractClause, Document, DocumentRevision, FlowdownMapping,
                        Obligation, ProjectEvent, Subcontract, User, Variation, VariationEvidence, VariationItem)
from app.routers.common import get_or_404, load_subcontract
from app.security import can_see_classification, current_user, project_roles, require
from app.services import variations as V

router = APIRouter(prefix="/api/v1", tags=["variations"])


def _dec(x):
    return None if x is None else str(x)


def _names(db: Session) -> dict:
    return {u.id: u.username for u in db.scalars(select(User))}


def variation_out(db: Session, v: Variation, names=None, full=False) -> dict:
    names = names or _names(db)
    sub = db.get(Subcontract, v.subcontract_id)
    est, basis = V.best_estimate(v)
    out = {"id": v.id, "number": v.variation_number, "title": v.title, "status": v.status, "category": v.category,
           "subcontract_id": v.subcontract_id, "subcontract_number": sub.subcontract_number, "project_id": v.project_id,
           "initiated_by": v.initiated_by, "instruction_reference": v.instruction_reference, "currency": v.currency,
           "claimed_amount": _dec(v.claimed_amount), "assessed_amount": _dec(v.assessed_amount),
           "recommended_amount": _dec(v.recommended_amount), "approved_amount": _dec(v.approved_amount),
           "claimed_days": v.claimed_days, "assessed_days": v.assessed_days, "recommended_days": v.recommended_days,
           "approved_days": v.approved_days, "estimate": _dec(est), "estimate_basis": basis,
           "entitlement_status": v.entitlement_status, "notice": V.notice_status(v),
           "created_by": names.get(v.created_by), "created_at": v.created_at.isoformat()}
    if full:
        ev = db.get(ProjectEvent, v.origin_event_id) if v.origin_event_id else None
        ob = db.get(Obligation, v.notice_obligation_id) if v.notice_obligation_id else None
        out.update({
            "description": v.description, "entitlement_basis": v.entitlement_basis,
            "entitlement_clauses": v.entitlement_clauses,
            "assessed_by": names.get(v.assessed_by), "recommended_by": names.get(v.recommended_by),
            "decided_by": names.get(v.decided_by), "decided_at": v.decided_at.isoformat() if v.decided_at else None,
            "decision_reason": v.decision_reason, "amendment_id": v.amendment_id,
            "origin_event": event_out(ev, names) if ev else None,
            "notice_obligation": {"id": ob.id, "number": ob.obligation_number, "type": ob.obligation_type,
                                  "statement": ob.statement, "source_wording": ob.source_wording} if ob else None,
            "items": [item_out(i) for i in v.items],
            "evidence": [{"id": e.id, "kind": e.kind, "note": e.note, "revision_id": e.document_revision_id,
                          **_doc_label(db, e.document_revision_id), "linked_by": names.get(e.linked_by)}
                         for e in db.scalars(select(VariationEvidence).where(VariationEvidence.variation_id == v.id))],
            "history": [{"at": a.occurred_at.isoformat(), "actor": names.get(a.actor_id, a.actor_id), "action": a.action,
                         "after": a.after, "reason": a.reason}
                        for a in db.scalars(select(AuditEvent).where(AuditEvent.object_id == v.id)
                                            .order_by(AuditEvent.id))],
        })
    return out


def _doc_label(db, rid):
    r = db.get(DocumentRevision, rid)
    d = db.get(Document, r.document_id) if r else None
    return {"document_number": d.document_number if d else "?", "revision": r.revision_label if r else "?",
            "title": d.title if d else ""}


def item_out(i: VariationItem) -> dict:
    return {"line_no": i.line_no, "description": i.description, "cost_type": i.cost_type,
            "pricing_basis": i.pricing_basis, "quantity": str(i.quantity), "unit": i.unit, "rate": str(i.rate),
            "amount": str(i.amount), "rate_reference": i.rate_reference}


def event_out(e: ProjectEvent, names) -> dict:
    return {"id": e.id, "number": e.event_number, "type": e.event_type, "occurred_on": e.occurred_on,
            "location": e.location, "description": e.description, "subcontract_id": e.subcontract_id,
            "source_revision_id": e.source_revision_id, "enabler_id": e.enabler_id,
            "recorded_by": names.get(e.recorded_by),
            "recorded_at": e.recorded_at.isoformat()}


def load_variation(db: Session, user: User, vid: str, action: str) -> tuple[Variation, Subcontract]:
    v = get_or_404(db, Variation, vid, "Variation")
    require(db, user, action, v.project_id)
    return v, db.get(Subcontract, v.subcontract_id)


def _revision_in_project(db: Session, user: User, rid: str, project_id: str) -> DocumentRevision:
    r = db.get(DocumentRevision, rid)
    d = db.get(Document, r.document_id) if r else None
    if not r or d.project_id != project_id:
        V.err(422, "DOCUMENT_NOT_IN_PROJECT", "That document revision does not belong to this project")
    if not can_see_classification(user, d.security_classification):
        V.err(403, "CLASSIFICATION", "Document classification exceeds your clearance")
    return r


def allowed_actions(db: Session, user: User, v: Variation) -> list[str]:
    roles = project_roles(db, user, v.project_id) or set()
    from app.security import PERMISSIONS
    can = lambda a: bool(roles & PERMISSIONS[a])  # noqa: E731
    acts = []
    for t in sorted(V.TRANSITIONS.get(v.status, set()), key=V.STATES.index):
        if can(V.TRANSITION_PERMISSION.get(t, "variation.assess")):
            acts.append(f"to:{t}")
    if v.status in V.EDITABLE_STATES and (can("variation.raise") or can("variation.assess")):
        acts.append("edit")
    if v.status == "NOTICE_REVIEW" and can("variation.notice_risk"):
        acts.append("notice_risk")
    if v.status == "ENTITLEMENT_ASSESSMENT" and can("variation.assess"):
        acts.append("entitlement")
    if v.status == "COST_AND_TIME_ASSESSMENT" and can("variation.assess"):
        acts.append("valuation")
    if v.status == "COMMERCIAL_REVIEW" and can("variation.recommend") and user.id != v.assessed_by:
        acts.append("recommend")
    if v.status == "APPROVAL_PENDING" and can("variation.approve") and user.id not in {v.created_by, v.assessed_by,
                                                                                         v.recommended_by}:
        acts.append("decide")
    if v.status == "APPROVED" and can("variation.incorporate"):
        acts.append("incorporate")
    if v.status not in ("CLOSED", "WITHDRAWN") and (can("variation.raise") or can("variation.assess")):
        acts.append("evidence")
    return acts


# ---------------------------------------------------------------- project events
class EventIn(BaseModel):
    event_type: str
    occurred_on: str
    description: str = Field(min_length=5)
    location: str = ""
    subcontract_id: str | None = None
    source_revision_id: str | None = None
    enabler_id: str | None = None


@router.post("/projects/{pid}/events", status_code=201)
def record_event(pid: str, body: EventIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "event.record", pid)
    if body.event_type not in V.EVENT_TYPES:
        V.err(422, "INVALID_EVENT_TYPE", f"Use one of {V.EVENT_TYPES}")
    if body.subcontract_id:
        s = get_or_404(db, Subcontract, body.subcontract_id, "Subcontract")
        if s.project_id != pid:
            V.err(422, "SUBCONTRACT_NOT_IN_PROJECT", "Subcontract belongs to another project")
    if body.source_revision_id:
        _revision_in_project(db, user, body.source_revision_id, pid)
    if body.enabler_id:
        from app.models import Enabler
        en = get_or_404(db, Enabler, body.enabler_id, "Enabler")
        if en.project_id != pid:
            V.err(422, "ENABLER_NOT_IN_PROJECT", "That enabler belongs to another project")
    n = (db.scalar(select(func.count()).select_from(ProjectEvent).where(ProjectEvent.project_id == pid)) or 0) + 1
    e = ProjectEvent(project_id=pid, subcontract_id=body.subcontract_id, event_number=f"EV-{n:03d}",
                     event_type=body.event_type, occurred_on=V.iso(body.occurred_on, "occurred_on"),
                     location=body.location, description=body.description,
                     source_revision_id=body.source_revision_id, enabler_id=body.enabler_id,
                     recorded_by=user.id)
    db.add(e)
    db.flush()
    audit.record(db, user.id, "event.recorded", "ProjectEvent", e.id, project_id=pid,
                 after={"number": e.event_number, "type": e.event_type, "occurred_on": e.occurred_on})
    db.commit()
    return event_out(e, _names(db))


@router.get("/projects/{pid}/events")
def list_events(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.read", pid)
    names = _names(db)
    linked = {}
    for v in db.scalars(select(Variation).where(Variation.project_id == pid, Variation.origin_event_id.is_not(None))):
        linked.setdefault(v.origin_event_id, []).append(v.variation_number)
    return [{**event_out(e, names), "variations": linked.get(e.id, [])}
            for e in db.scalars(select(ProjectEvent).where(ProjectEvent.project_id == pid)
                                .order_by(ProjectEvent.occurred_on.desc(), ProjectEvent.event_number.desc()))]


# ---------------------------------------------------------------- raise and list
class VariationIn(BaseModel):
    title: str = Field(min_length=5, max_length=300)
    description: str = ""
    category: str | None = None
    initiated_by: str = "EPC"
    origin_event_id: str | None = None
    instruction_reference: str = ""
    event_date: str | None = None          # starts the notice clock; defaults to the origin event's date
    claimed_amount: str | None = None
    claimed_days: int | None = Field(default=None, ge=0)


@router.post("/subcontracts/{sid}/variations", status_code=201)
def raise_variation(sid: str, body: VariationIn, user: User = Depends(current_user), db: Session = Depends(get_db),
                    idempotency_key: str | None = Header(default=None)):
    sub = load_subcontract(db, user, sid, "variation.raise")
    payload = {"sid": sid, **body.model_dump()}
    replay = audit.idempotent_lookup(db, idempotency_key, user.id, payload)
    if replay:
        return replay
    V.require_executed(sub)
    if body.category and body.category not in V.CATEGORIES:
        V.err(422, "INVALID_CATEGORY", f"Use one of {V.CATEGORIES}")
    if body.initiated_by not in ("EPC", "SUBCONTRACTOR", "EMPLOYER"):
        V.err(422, "INVALID_INITIATOR", "EPC, SUBCONTRACTOR or EMPLOYER")
    clock = V.iso(body.event_date, "event_date")
    if body.origin_event_id:
        ev = get_or_404(db, ProjectEvent, body.origin_event_id, "Event")
        if ev.project_id != sub.project_id or (ev.subcontract_id and ev.subcontract_id != sid):
            V.err(422, "EVENT_NOT_FOR_SUBCONTRACT", "That event belongs to another project or subcontract")
        clock = clock or ev.occurred_on
    v = Variation(project_id=sub.project_id, subcontract_id=sid, variation_number=V.next_number(db, sid),
                  title=body.title, description=body.description, category=body.category,
                  initiated_by=body.initiated_by, origin_event_id=body.origin_event_id,
                  instruction_reference=body.instruction_reference, currency=sub.currency, notice_clock_start=clock,
                  claimed_amount=V.money(body.claimed_amount), claimed_days=body.claimed_days, created_by=user.id)
    V.set_clock(db, v, sub)
    db.add(v)
    try:
        db.flush()
    except IntegrityError:   # two people raising at the same instant: the number is unique per subcontract
        db.rollback()
        raise HTTPException(409, {"code": "RETRY", "message": "Variation number taken; submit again"})
    audit.record(db, user.id, "variation.raised", "Variation", v.id, project_id=v.project_id,
                 after={"number": v.variation_number, "title": v.title, "claimed_amount": v.claimed_amount,
                        "notice_due_on": v.notice_due_on, "origin_event_id": v.origin_event_id})
    out = variation_out(db, v)
    audit.idempotent_store(db, idempotency_key, user.id, payload, out)
    db.commit()
    return out


@router.get("/projects/{pid}/variations")
def list_variations(pid: str, subcontract_id: str | None = None, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    require(db, user, "project.read", pid)
    q = select(Variation).where(Variation.project_id == pid)
    if subcontract_id:
        q = q.where(Variation.subcontract_id == subcontract_id)
    names = _names(db)
    rows = [variation_out(db, v, names) for v in db.scalars(q.order_by(Variation.created_at))]
    subs = {r["subcontract_id"] for r in rows} | ({subcontract_id} if subcontract_id else set())
    positions = {s: {"subcontract_number": db.get(Subcontract, s).subcontract_number,
                     **V.position(db, db.get(Subcontract, s))} for s in subs if db.get(Subcontract, s)}
    return {"variations": rows,
            "summary": {"open": sum(r["status"] in V.OPEN_STATES for r in rows),
                        "notice_at_risk": sum(r["notice"]["at_risk"] and r["status"] in V.OPEN_STATES for r in rows),
                        "awaiting_approval": sum(r["status"] == "APPROVAL_PENDING" for r in rows),
                        "approved_not_incorporated": sum(r["status"] == "APPROVED" for r in rows)},
            "positions": positions,
            "reference": {"categories": V.CATEGORIES, "event_types": V.EVENT_TYPES, "entitlement": V.ENTITLEMENT,
                          "cost_types": V.COST_TYPES, "pricing_bases": V.PRICING_BASES,
                          "evidence_kinds": V.EVIDENCE_KINDS, "states": V.STATES}}


@router.get("/variations/{vid}")
def get_variation(vid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v, _ = load_variation(db, user, vid, "project.read")
    return {**variation_out(db, v, full=True), "actions": allowed_actions(db, user, v)}


@router.get("/subcontracts/{sid}/commercial-position")
def commercial_position(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = load_subcontract(db, user, sid, "project.read")
    return V.position(db, sub)


# ---------------------------------------------------------------- edits and decisions
class VariationPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    instruction_reference: str | None = None
    event_date: str | None = None
    notice_given_on: str | None = None
    notice_revision_id: str | None = None
    claimed_amount: str | None = None
    claimed_days: int | None = Field(default=None, ge=0)


@router.patch("/variations/{vid}")
def edit_variation(vid: str, body: VariationPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v, sub = load_variation(db, user, vid, "project.read")
    roles = project_roles(db, user, v.project_id) or set()
    from app.security import PERMISSIONS
    if not roles & (PERMISSIONS["variation.raise"] | PERMISSIONS["variation.assess"]):
        V.err(403, "FORBIDDEN", "Missing permission variation.raise")
    if v.status not in V.EDITABLE_STATES:
        V.err(409, "LOCKED", f"A variation in {v.status} can no longer be edited")
    data = body.model_dump(exclude_unset=True)
    before = audit.snapshot(v, ["title", "category", "notice_clock_start", "notice_given_on", "claimed_amount",
                                "claimed_days"])
    if "category" in data and data["category"] not in V.CATEGORIES:
        V.err(422, "INVALID_CATEGORY", f"Use one of {V.CATEGORIES}")
    if "notice_revision_id" in data and data["notice_revision_id"]:
        _revision_in_project(db, user, data["notice_revision_id"], v.project_id)
    if ("notice_given_on" in data or "event_date" in data) and v.status not in ("IDENTIFIED", "CLASSIFIED", "NOTICE_REVIEW"):
        V.err(409, "NOTICE_LOCKED", "Notice facts are fixed once entitlement assessment starts; step back to notice review")
    for k in ("title", "description", "category", "instruction_reference", "claimed_days", "notice_revision_id"):
        if k in data:
            setattr(v, k, data[k])
    if "claimed_amount" in data:
        v.claimed_amount = V.money(data["claimed_amount"])
    if "notice_given_on" in data:
        v.notice_given_on = V.iso(data["notice_given_on"], "notice_given_on")
    if "event_date" in data:
        v.notice_clock_start = V.iso(data["event_date"], "event_date")
        V.set_clock(db, v, sub)
    if v.notice_revision_id and not db.scalar(select(VariationEvidence).where(
            VariationEvidence.variation_id == v.id, VariationEvidence.document_revision_id == v.notice_revision_id,
            VariationEvidence.kind == "NOTICE")):
        db.add(VariationEvidence(variation_id=v.id, document_revision_id=v.notice_revision_id, kind="NOTICE",
                                 note="Contractual notice", linked_by=user.id))
    audit.record(db, user.id, "variation.updated", "Variation", v.id, project_id=v.project_id, before=before,
                 after=audit.snapshot(v, list(before)))
    db.commit()
    return get_variation(vid, user, db)


class TransitionIn(BaseModel):
    target: str
    reason: str | None = None


@router.post("/variations/{vid}/transitions")
def transition(vid: str, body: TransitionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v, _ = load_variation(db, user, vid, "project.read")
    require(db, user, V.TRANSITION_PERMISSION.get(body.target, "variation.assess"), v.project_id)
    V.move(db, v, body.target, user, body.reason)
    db.commit()
    return get_variation(vid, user, db)


class NoticeRiskIn(BaseModel):
    decision: str            # PROCEED_DESPITE_NOTICE_RISK | NOTICE_NOT_REQUIRED
    reason: str = Field(min_length=15)


@router.post("/variations/{vid}/notice-risk")
def notice_risk(vid: str, body: NoticeRiskIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """VAR-01: a late, missing or unclear notice is passed only by a named person with a recorded reason.
    This records a risk decision; it does not waive any right the EPC contractor holds."""
    v, _ = load_variation(db, user, vid, "variation.notice_risk")
    if v.status != "NOTICE_REVIEW":
        V.err(409, "NOT_IN_NOTICE_REVIEW", f"Variation is {v.status}")
    if body.decision not in ("PROCEED_DESPITE_NOTICE_RISK", "NOTICE_NOT_REQUIRED"):
        V.err(422, "INVALID_DECISION", "PROCEED_DESPITE_NOTICE_RISK or NOTICE_NOT_REQUIRED")
    n = V.notice_status(v)
    if n["status"] in ("GIVEN_ON_TIME", "NOT_REQUIRED"):
        V.err(409, "NO_NOTICE_RISK", "Notice was given on time (or is not required); no decision is needed")
    if body.decision == "PROCEED_DESPITE_NOTICE_RISK" and n["status"] == "PENDING":
        V.err(409, "NOTICE_STILL_OPEN", "The notice period has not expired; record the notice when it arrives")
    v.notice_risk_decision = {"decision": body.decision, "reason": body.reason, "by": user.username,
                              "at": V.now().isoformat(), "notice_status_at_decision": n["status"]}
    audit.record(db, user.id, "variation.notice_risk_decided", "Variation", v.id, project_id=v.project_id,
                 after=v.notice_risk_decision, reason=body.reason)
    db.commit()
    return get_variation(vid, user, db)


class EntitlementIn(BaseModel):
    status: str
    basis: str = Field(min_length=15)
    clause_references: list[str] = []


@router.post("/variations/{vid}/entitlement")
def entitlement(vid: str, body: EntitlementIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v, sub = load_variation(db, user, vid, "variation.assess")
    if v.status != "ENTITLEMENT_ASSESSMENT":
        V.err(409, "NOT_IN_ENTITLEMENT", f"Variation is {v.status}")
    if body.status not in V.ENTITLEMENT[1:]:
        V.err(422, "INVALID_ENTITLEMENT", f"Use one of {V.ENTITLEMENT[1:]}")
    if body.status in ("ENTITLED", "PARTIALLY_ENTITLED") and not body.clause_references:
        V.err(422, "BASIS_REQUIRED", "Cite the subcontract clause(s) giving entitlement")
    known = {c.clause_reference for c in db.scalars(
        select(ContractClause).join(FlowdownMapping, FlowdownMapping.source_clause_id == ContractClause.id)
        .where(FlowdownMapping.subcontract_id == sub.id))}
    unknown = [c for c in body.clause_references if c not in known]
    v.entitlement_status, v.entitlement_basis, v.entitlement_clauses = body.status, body.basis, body.clause_references
    v.assessed_by = user.id
    audit.record(db, user.id, "variation.entitlement_assessed", "Variation", v.id, project_id=v.project_id,
                 after={"status": body.status, "clauses": body.clause_references}, reason=body.basis)
    db.commit()
    out = get_variation(vid, user, db)
    out["warnings"] = ([f"Clause(s) not found in this subcontract's flow-down: {', '.join(unknown)}"]
                       if unknown else [])
    return out


class ItemIn(BaseModel):
    description: str = Field(min_length=3)
    cost_type: str = "OTHER"
    pricing_basis: str = "CONTRACT_RATE"
    quantity: str
    unit: str = Field(min_length=1)
    rate: str
    rate_reference: str = ""


class ValuationIn(BaseModel):
    items: list[ItemIn]
    assessed_days: int | None = Field(default=None, ge=0)


@router.put("/variations/{vid}/valuation")
def valuation(vid: str, body: ValuationIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Replace the priced lines; the server computes every amount and the assessed total."""
    v, _ = load_variation(db, user, vid, "variation.assess")
    if v.status != "COST_AND_TIME_ASSESSMENT":
        V.err(409, "NOT_IN_VALUATION", f"Variation is {v.status}")
    lines = []
    for n, it in enumerate(body.items, 1):
        if it.cost_type not in V.COST_TYPES or it.pricing_basis not in V.PRICING_BASES:
            V.err(422, "INVALID_ITEM", f"Line {n}: cost type {V.COST_TYPES}; pricing basis {V.PRICING_BASES}")
        try:
            qty, rate = Decimal(it.quantity), V.money(it.rate)
        except Exception:  # noqa: BLE001
            V.err(422, "INVALID_AMOUNT", f"Line {n}: quantity and rate must be numbers")
        if v.category != "SCOPE_OMISSION" and (qty < 0 or rate < 0):
            V.err(422, "NEGATIVE_LINE", f"Line {n}: negative values are only for scope omissions")
        if it.pricing_basis in ("STAR_RATE", "QUOTATION") and not it.rate_reference.strip():
            V.err(422, "RATE_REFERENCE_REQUIRED", f"Line {n}: a {it.pricing_basis.lower()} needs its reference")
        lines.append(VariationItem(line_no=n, description=it.description, cost_type=it.cost_type,
                                   pricing_basis=it.pricing_basis, quantity=qty.quantize(Decimal("0.001")), unit=it.unit,
                                   rate=rate, amount=(qty * rate).quantize(V.CENT, rounding="ROUND_HALF_UP"),
                                   rate_reference=it.rate_reference))
    before = {"assessed_amount": v.assessed_amount, "assessed_days": v.assessed_days}
    v.items = lines
    v.assessed_amount = sum((x.amount for x in lines), Decimal("0.00")) if lines else None
    v.assessed_days = body.assessed_days
    v.assessed_by = user.id
    audit.record(db, user.id, "variation.valued", "Variation", v.id, project_id=v.project_id, before=before,
                 after={"assessed_amount": v.assessed_amount, "assessed_days": v.assessed_days, "lines": len(lines)})
    db.commit()
    return get_variation(vid, user, db)


class RecommendIn(BaseModel):
    amount: str | None = None
    days: int | None = Field(default=None, ge=0)
    comment: str = ""


@router.post("/variations/{vid}/recommend")
def recommend(vid: str, body: RecommendIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v, _ = load_variation(db, user, vid, "variation.recommend")
    if v.status != "COMMERCIAL_REVIEW":
        V.err(409, "NOT_IN_COMMERCIAL_REVIEW", f"Variation is {v.status}")
    if user.id == v.assessed_by:
        V.err(403, "SEGREGATION_OF_DUTIES", "The assessor cannot recommend their own valuation")
    amt = V.money(body.amount) if body.amount is not None else v.assessed_amount
    days = body.days if body.days is not None else v.assessed_days
    if (amt != v.assessed_amount or days != v.assessed_days) and len(body.comment.strip()) < 10:
        V.err(422, "REASON_REQUIRED", "Explain why the recommendation differs from the assessment")
    v.recommended_amount, v.recommended_days, v.recommended_by = amt, days, user.id
    v.status = "APPROVAL_PENDING"
    audit.record(db, user.id, "variation.recommended", "Variation", v.id, project_id=v.project_id,
                 before={"status": "COMMERCIAL_REVIEW"},
                 after={"status": v.status, "recommended_amount": amt, "recommended_days": days}, reason=body.comment)
    db.commit()
    return get_variation(vid, user, db)


class DecisionIn(BaseModel):
    decision: str            # APPROVE | REJECT | RETURN
    amount: str | None = None
    days: int | None = Field(default=None, ge=0)
    reason: str = ""


@router.post("/variations/{vid}/decision")
def decision(vid: str, body: DecisionIn, user: User = Depends(current_user), db: Session = Depends(get_db),
             idempotency_key: str | None = Header(default=None)):
    v, _ = load_variation(db, user, vid, "variation.approve")
    payload = {"vid": vid, **body.model_dump()}
    replay = audit.idempotent_lookup(db, idempotency_key, user.id, payload)
    if replay:
        return replay
    V.decide(db, v, user, body.decision, body.amount, body.days, body.reason)
    db.commit()
    out = get_variation(vid, user, db)
    audit.idempotent_store(db, idempotency_key, user.id, payload, out)
    db.commit()
    return out


@router.post("/variations/{vid}/incorporate")
def incorporate(vid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v, sub = load_variation(db, user, vid, "variation.incorporate")
    V.require_executed(sub)
    a = V.incorporate(db, v, sub, user)
    db.commit()
    return {**get_variation(vid, user, db), "amendment": {"id": a.id, "number": a.amendment_number,
                                                           "old_value": a.old_value, "new_value": a.new_value} if a else None,
            "position": V.position(db, sub)}


class EvidenceIn(BaseModel):
    document_revision_id: str
    kind: str
    note: str = ""


@router.post("/variations/{vid}/evidence", status_code=201)
def link_evidence(vid: str, body: EvidenceIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v, _ = load_variation(db, user, vid, "project.read")
    if "evidence" not in allowed_actions(db, user, v):
        V.err(403, "FORBIDDEN", "You cannot add evidence to this variation")
    if body.kind not in V.EVIDENCE_KINDS:
        V.err(422, "INVALID_KIND", f"Use one of {V.EVIDENCE_KINDS}")
    _revision_in_project(db, user, body.document_revision_id, v.project_id)
    if db.scalar(select(VariationEvidence).where(VariationEvidence.variation_id == v.id,
                                                 VariationEvidence.document_revision_id == body.document_revision_id,
                                                 VariationEvidence.kind == body.kind)):
        V.err(409, "ALREADY_LINKED", "That document is already linked for this purpose")
    e = VariationEvidence(variation_id=v.id, document_revision_id=body.document_revision_id, kind=body.kind,
                          note=body.note, linked_by=user.id)
    db.add(e)
    db.flush()
    audit.record(db, user.id, "variation.evidence_linked", "Variation", v.id, project_id=v.project_id,
                 after={"kind": body.kind, "revision_id": body.document_revision_id})
    db.commit()
    return {"id": e.id}


@router.get("/variations/{vid}/trace")
def trace(vid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Event -> notice obligation -> source clause -> evidence -> decision -> amendment (knowledge-graph view)."""
    v, sub = load_variation(db, user, vid, "project.read")
    names = _names(db)
    nodes, edges = [], []

    def node(kind, nid, label, **data):
        nodes.append({"kind": kind, "id": nid, "label": label, **data})

    node("Variation", v.id, f"{v.variation_number} {v.title}", status=v.status)
    node("Subcontract", sub.id, sub.subcontract_number)
    edges.append((v.id, "VARIES", sub.id))
    if v.origin_event_id:
        e = db.get(ProjectEvent, v.origin_event_id)
        node("ProjectEvent", e.id, f"{e.event_number} {e.event_type} {e.occurred_on}")
        edges.append((e.id, "MAY_GIVE_RISE_TO", v.id))
        if e.enabler_id:
            from app.models import Enabler
            en = db.get(Enabler, e.enabler_id)
            node("Enabler", en.id, f"{en.enabler_number} {en.name} (planned {en.planned_date}, "
                 f"available {en.actual_date or en.forecast_date or en.planned_date})")
            edges.append((en.id, "LATENESS_RECORDED_IN", e.id))
    if v.notice_obligation_id:
        ob = db.get(Obligation, v.notice_obligation_id)
        node("Obligation", ob.id, f"{ob.obligation_number} notice within {ob.notice_period_days} days")
        edges.append((v.id, "NOTICE_GOVERNED_BY", ob.id))
        if ob.source_mapping_id:
            m = db.get(FlowdownMapping, ob.source_mapping_id)
            c = db.get(ContractClause, m.source_clause_id)
            node("SourceClause", c.id, f"Main contract {c.clause_reference} {c.heading}", disposition=m.disposition)
            edges.append((c.id, "FLOWS_DOWN_AS", ob.id))
    for ev in db.scalars(select(VariationEvidence).where(VariationEvidence.variation_id == v.id)):
        lbl = _doc_label(db, ev.document_revision_id)
        node("Evidence", ev.id, f"{lbl['document_number']} rev {lbl['revision']} ({ev.kind.lower()})")
        edges.append((v.id, "EVIDENCED_BY", ev.id))
    if v.decided_by:
        node("Approval", f"decision-{v.id}", f"{v.status if v.status in ('REJECTED', 'WITHDRAWN') else 'APPROVED'} by "
             f"{names.get(v.decided_by)}")
        edges.append((v.id, "APPROVED_BY", f"decision-{v.id}"))
    if v.amendment_id:
        a = db.get(Amendment, v.amendment_id)
        node("Amendment", a.id, f"Amendment {a.amendment_number}: {a.old_value} -> {a.new_value}")
        edges.append((v.id, "AFFECTS", a.id))
    return {"nodes": nodes, "edges": [{"from": f, "type": t, "to": to} for f, t, to in edges]}
