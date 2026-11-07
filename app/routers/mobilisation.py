"""Mobilisation checklist and site-enabler register (blueprint 5.2 and 6.1, acceptance WF-02, ADR-0009)."""
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.models import (Document, DocumentRevision, Enabler, EnablerDependency, EnablerLink, MobilisationItem,
                        ProjectEvent, Subcontract, User, now)
from app.routers.common import get_or_404, load_subcontract
from app.security import PERMISSIONS, can_see_classification, current_user, project_roles, require
from app.services import mobilisation as M

router = APIRouter(prefix="/api/v1", tags=["mobilisation"])


def _names(db):
    return {u.id: u.username for u in db.scalars(select(User))}


def _doc(db, rid):
    if not rid:
        return None
    r = db.get(DocumentRevision, rid)
    d = db.get(Document, r.document_id) if r else None
    return {"revision_id": rid, "document_number": d.document_number if d else "?",
            "revision": r.revision_label if r else "?", "title": d.title if d else ""}


def _revision_in_project(db, user, rid, project_id):
    r = db.get(DocumentRevision, rid)
    d = db.get(Document, r.document_id) if r else None
    if not r or d.project_id != project_id:
        M.err(422, "DOCUMENT_NOT_IN_PROJECT", "That document revision does not belong to this project")
    if not can_see_classification(user, d.security_classification):
        M.err(403, "CLASSIFICATION", "Document classification exceeds your clearance")
    return r


def _roles(db, user, project_id):
    return project_roles(db, user, project_id) or set()


# ---------------------------------------------------------------- checklist
def item_out(db, i: MobilisationItem, names, user=None, roles=frozenset()) -> dict:
    en = db.get(Enabler, i.enabler_id) if i.enabler_id else None
    out = {"id": i.id, "code": i.item_code, "line_no": i.line_no, "category": i.category, "title": i.title,
           "responsible_party": i.responsible_party, "mandatory": i.mandatory, "due_date": i.due_date,
           "overdue": i.status not in M.CLOSED and bool(i.due_date) and i.due_date < date.today().isoformat(),
           "evidence_required": i.evidence_required, "verifier_roles": i.verifier_roles, "status": i.status,
           "evidence": _doc(db, i.evidence_revision_id), "submitted_by": names.get(i.submitted_by),
           "verified_by": names.get(i.verified_by), "review_comment": i.review_comment, "exception": i.exception,
           "source": i.source,
           "enabler": {"id": en.id, "number": en.enabler_number, "name": en.name, "status": en.status} if en else None}
    if user is not None:
        acts = []
        if i.status in ("OPEN",) and roles & PERMISSIONS["mobilisation.submit"]:
            acts.append("submit")
        if i.status == "SUBMITTED" and roles & set(i.verifier_roles) and user.id != i.submitted_by:
            acts.append("verify")
        if i.status not in M.CLOSED and roles & PERMISSIONS["mobilisation.exception"]:
            acts.append("waive")
        if i.status == "OPEN" and roles & PERMISSIONS["mobilisation.manage"]:
            acts.append("link_enabler")
        out["actions"] = acts
    return out


@router.get("/subcontracts/{sid}/mobilisation")
def checklist(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = load_subcontract(db, user, sid, "project.read")
    names, roles = _names(db), _roles(db, user, sub.project_id)
    items = db.scalars(select(MobilisationItem).where(MobilisationItem.subcontract_id == sid)
                       .order_by(MobilisationItem.line_no))
    return {"subcontract_id": sid, "subcontract_number": sub.subcontract_number, "status": sub.status,
            "discipline": sub.discipline, "readiness": M.readiness(db, sub),
            "items": [item_out(db, i, names, user, roles) for i in items],
            "can_generate": sub.status in ("EXECUTED", "ACTIVE") and bool(roles & PERMISSIONS["mobilisation.manage"]),
            "can_start_work": sub.status == "EXECUTED" and "contracts_manager" in roles,
            "categories": M.CATEGORIES}


@router.post("/subcontracts/{sid}/mobilisation/generate")
def generate(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = load_subcontract(db, user, sid, "mobilisation.manage")
    if sub.status not in ("EXECUTED", "ACTIVE"):
        M.err(409, "SUBCONTRACT_NOT_EXECUTED", "The checklist is generated once the subcontract is executed")
    added = M.generate(db, sub, user.id)
    db.commit()
    return {"added": added, "readiness": M.readiness(db, sub)}


class ItemIn(BaseModel):
    title: str = Field(min_length=5)
    category: str = "OTHER"
    responsible_party: str = "SUBCONTRACTOR"
    mandatory: bool = True
    due_date: str | None = None
    evidence_required: str = Field(min_length=3)
    verifier_roles: list[str] = Field(min_length=1)


@router.post("/subcontracts/{sid}/mobilisation/items", status_code=201)
def add_item(sid: str, body: ItemIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = load_subcontract(db, user, sid, "mobilisation.manage")
    if sub.status not in ("EXECUTED", "ACTIVE"):
        M.err(409, "SUBCONTRACT_NOT_EXECUTED", "Add items once the subcontract is executed")
    if body.category not in M.CATEGORIES or body.responsible_party not in ("SUBCONTRACTOR", "EPC", "EMPLOYER"):
        M.err(422, "INVALID_ITEM", f"Category one of {M.CATEGORIES}; responsible SUBCONTRACTOR, EPC or EMPLOYER")
    from app.routers.auth import ALL_ROLES
    if set(body.verifier_roles) - set(ALL_ROLES) or "system_admin" in body.verifier_roles:
        M.err(422, "INVALID_ROLE", "Unknown verifier role")
    existing = list(db.scalars(select(MobilisationItem).where(MobilisationItem.subcontract_id == sid)))
    n = sum(1 for i in existing if i.source == "MANUAL") + 1
    i = MobilisationItem(subcontract_id=sid, item_code=f"ADD-{n:02d}", line_no=len(existing) + 1,
                         category=body.category, title=body.title, responsible_party=body.responsible_party,
                         mandatory=body.mandatory, due_date=M.iso(body.due_date, "due_date"),
                         evidence_required=body.evidence_required, verifier_roles=body.verifier_roles, source="MANUAL")
    db.add(i)
    db.flush()
    audit.record(db, user.id, "mobilisation.item_added", "MobilisationItem", i.id, project_id=sub.project_id,
                 after={"code": i.item_code, "title": i.title, "mandatory": i.mandatory})
    db.commit()
    return item_out(db, i, _names(db))


def _load_item(db, user, iid):
    i = get_or_404(db, MobilisationItem, iid, "Mobilisation item")
    sub = db.get(Subcontract, i.subcontract_id)
    require(db, user, "project.read", sub.project_id)
    return i, sub


class SubmitIn(BaseModel):
    document_revision_id: str | None = None
    note: str = ""


@router.post("/mobilisation-items/{iid}/submit")
def submit(iid: str, body: SubmitIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    i, sub = _load_item(db, user, iid)
    require(db, user, "mobilisation.submit", sub.project_id)
    if i.status != "OPEN":
        M.err(409, "NOT_OPEN", f"Item is {i.status}")
    if i.enabler_id:
        en = db.get(Enabler, i.enabler_id)
        if en.status != "READY":
            M.err(409, "ENABLER_NOT_READY", f"{en.enabler_number} {en.name} is {en.status}; this item closes when "
                  "the enabler is verified ready")
        rid = body.document_revision_id or en.readiness_revision_id
    else:
        rid = body.document_revision_id
    if not rid:   # WF-02: nothing closes without evidence
        M.err(422, "EVIDENCE_REQUIRED", f"Attach the evidence: {i.evidence_required}")
    _revision_in_project(db, user, rid, sub.project_id)
    i.evidence_revision_id, i.status, i.submitted_by, i.submitted_at = rid, "SUBMITTED", user.id, now()
    i.review_comment = body.note
    audit.record(db, user.id, "mobilisation.item_submitted", "MobilisationItem", i.id, project_id=sub.project_id,
                 after={"code": i.item_code, "evidence_revision_id": rid})
    db.commit()
    return item_out(db, i, _names(db))


class VerifyIn(BaseModel):
    decision: str          # ACCEPT | REJECT
    comment: str = ""


@router.post("/mobilisation-items/{iid}/verify")
def verify(iid: str, body: VerifyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    i, sub = _load_item(db, user, iid)
    if i.status != "SUBMITTED":
        M.err(409, "NOT_SUBMITTED", f"Item is {i.status}")
    if not _roles(db, user, sub.project_id) & set(i.verifier_roles):
        M.err(403, "FORBIDDEN", f"Verified by: {', '.join(i.verifier_roles)}")
    if user.id == i.submitted_by:
        M.err(403, "SEGREGATION_OF_DUTIES", "Whoever submitted the evidence cannot verify it")
    if body.decision not in ("ACCEPT", "REJECT"):
        M.err(422, "INVALID_DECISION", "ACCEPT or REJECT")
    if body.decision == "REJECT" and len(body.comment.strip()) < 5:
        M.err(422, "REASON_REQUIRED", "Say what is missing")
    if body.decision == "ACCEPT":
        i.status, i.verified_by, i.verified_at = "VERIFIED", user.id, now()
    else:
        i.status, i.evidence_revision_id = "OPEN", None
    i.review_comment = body.comment
    audit.record(db, user.id, f"mobilisation.item_{'verified' if body.decision == 'ACCEPT' else 'rejected'}",
                 "MobilisationItem", i.id, project_id=sub.project_id, after={"code": i.item_code, "status": i.status},
                 reason=body.comment)
    db.commit()
    return item_out(db, i, _names(db))


class ExceptionIn(BaseModel):
    reason: str = Field(min_length=15)
    expires_on: str | None = None       # e.g. "insurance certificate to follow by ..."


@router.post("/mobilisation-items/{iid}/exception")
def waive(iid: str, body: ExceptionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """WF-02: closing without evidence only by a contracts approver, with a recorded reason."""
    i, sub = _load_item(db, user, iid)
    require(db, user, "mobilisation.exception", sub.project_id)
    if i.status in M.CLOSED:
        M.err(409, "ALREADY_CLOSED", f"Item is {i.status}")
    i.exception = {"reason": body.reason, "by": user.username, "at": now().isoformat(),
                   "expires_on": M.iso(body.expires_on, "expires_on"), "status_before": i.status}
    i.status, i.verified_by, i.verified_at = "WAIVED", user.id, now()
    audit.record(db, user.id, "mobilisation.item_waived", "MobilisationItem", i.id, project_id=sub.project_id,
                 after={"code": i.item_code, **i.exception}, reason=body.reason)
    db.commit()
    return item_out(db, i, _names(db))


class LinkEnablerIn(BaseModel):
    enabler_id: str | None


@router.post("/mobilisation-items/{iid}/enabler")
def link_item_enabler(iid: str, body: LinkEnablerIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    i, sub = _load_item(db, user, iid)
    require(db, user, "mobilisation.manage", sub.project_id)
    if i.status != "OPEN":
        M.err(409, "NOT_OPEN", f"Item is {i.status}")
    if body.enabler_id:
        en = get_or_404(db, Enabler, body.enabler_id, "Enabler")
        if en.project_id != sub.project_id:
            M.err(422, "ENABLER_NOT_IN_PROJECT", "That enabler belongs to another project")
    i.enabler_id = body.enabler_id
    audit.record(db, user.id, "mobilisation.item_enabler_linked", "MobilisationItem", i.id, project_id=sub.project_id,
                 after={"enabler_id": body.enabler_id})
    db.commit()
    return item_out(db, i, _names(db))


# ---------------------------------------------------------------- enablers
def enabler_out(db, e: Enabler, names, subs=None) -> dict:
    subs = subs or {}
    links = [M.link_view(e, l) for l in db.scalars(select(EnablerLink).where(EnablerLink.enabler_id == e.id)
                                                   .order_by(EnablerLink.need_by_date))]
    for lv in links:
        s = subs.get(lv["subcontract_id"]) or db.get(Subcontract, lv["subcontract_id"])
        lv["subcontract_number"] = s.subcontract_number
    deps = [db.get(Enabler, d.depends_on_id) for d in
            db.scalars(select(EnablerDependency).where(EnablerDependency.enabler_id == e.id))]
    events = [{"id": x.id, "number": x.event_number, "occurred_on": x.occurred_on}
              for x in db.scalars(select(ProjectEvent).where(ProjectEvent.enabler_id == e.id))]
    worst = max((l["late_days"] for l in links), default=0)
    return {"id": e.id, "number": e.enabler_number, "type": e.enabler_type, "name": e.name, "location": e.location,
            "capacity": e.capacity, "responsible_party": e.responsible_party, "cost_allocation": e.cost_allocation,
            "owner": names.get(e.owner_id), "planned_date": e.planned_date, "forecast_date": e.forecast_date,
            "actual_date": e.actual_date, "available": M.effective_date(e), "status": e.status,
            "readiness_evidence": _doc(db, e.readiness_revision_id), "claimed_by": names.get(e.claimed_by),
            "verified_by": names.get(e.verified_by), "notes": e.notes, "links": links,
            "prerequisites": [{"id": d.id, "number": d.enabler_number, "status": d.status} for d in deps],
            "events": events, "worst_late_days": worst,
            "flag": ("LATE" if any(l["state"] == "LATE" for l in links) else
                     "AT_RISK" if any(l["state"] == "AT_RISK" for l in links) else
                     "READY" if e.status == "READY" else "ON_TRACK")}


@router.get("/projects/{pid}/enablers")
def list_enablers(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.read", pid)
    names = _names(db)
    subs = {s.id: s for s in db.scalars(select(Subcontract).where(Subcontract.project_id == pid))}
    rows = [enabler_out(db, e, names, subs) for e in
            db.scalars(select(Enabler).where(Enabler.project_id == pid).order_by(Enabler.enabler_number))]
    roles = _roles(db, user, pid)
    return {"enablers": rows,
            "summary": {"total": len(rows), "ready": sum(r["status"] == "READY" for r in rows),
                        "at_risk": sum(r["flag"] == "AT_RISK" for r in rows),
                        "late": sum(r["flag"] == "LATE" for r in rows),
                        "awaiting_verification": sum(r["status"] == "READY_CLAIMED" for r in rows)},
            "types": M.ENABLER_TYPES,
            "can_manage": bool(roles & PERMISSIONS["enabler.manage"]),
            "can_verify": bool(roles & PERMISSIONS["enabler.verify"]),
            "can_record_event": bool(roles & PERMISSIONS["event.record"])}


class EnablerIn(BaseModel):
    enabler_type: str
    name: str = Field(min_length=3)
    planned_date: str
    location: str = ""
    capacity: str = ""
    responsible_party: str = "EPC"
    cost_allocation: str = "EPC"
    owner_username: str | None = None
    notes: str = ""
    depends_on: list[str] = []


def _check_parties(body):
    if body.responsible_party not in ("EPC", "EMPLOYER", "SUBCONTRACTOR", "OTHER") or \
            body.cost_allocation not in ("EPC", "EMPLOYER", "SUBCONTRACTOR", "SHARED"):
        M.err(422, "INVALID_PARTY", "responsible: EPC/EMPLOYER/SUBCONTRACTOR/OTHER; cost: EPC/EMPLOYER/SUBCONTRACTOR/SHARED")


@router.post("/projects/{pid}/enablers", status_code=201)
def create_enabler(pid: str, body: EnablerIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "enabler.manage", pid)
    if body.enabler_type not in M.ENABLER_TYPES:
        M.err(422, "INVALID_TYPE", f"Use one of {M.ENABLER_TYPES}")
    _check_parties(body)
    owner = None
    if body.owner_username:
        owner = db.scalar(select(User).where(User.username == body.owner_username))
        if not owner:
            M.err(422, "UNKNOWN_USER", "Owner not found")
    e = Enabler(project_id=pid, enabler_number=M.next_enabler_number(db, pid), enabler_type=body.enabler_type,
                name=body.name, location=body.location, capacity=body.capacity,
                responsible_party=body.responsible_party, cost_allocation=body.cost_allocation,
                owner_id=owner.id if owner else user.id, planned_date=M.iso(body.planned_date, "planned_date"),
                notes=body.notes, created_by=user.id)
    db.add(e)
    db.flush()
    for d in body.depends_on:
        dep = get_or_404(db, Enabler, d, "Enabler")
        if dep.project_id != pid or dep.id == e.id:
            M.err(422, "INVALID_DEPENDENCY", "Prerequisite must be another enabler of this project")
        db.add(EnablerDependency(enabler_id=e.id, depends_on_id=dep.id))
    audit.record(db, user.id, "enabler.created", "Enabler", e.id, project_id=pid,
                 after={"number": e.enabler_number, "name": e.name, "planned_date": e.planned_date})
    db.commit()
    return enabler_out(db, e, _names(db))


class EnablerPatch(BaseModel):
    forecast_date: str | None = None
    planned_date: str | None = None
    reason: str = ""
    notes: str | None = None


@router.patch("/enablers/{eid}")
def update_enabler(eid: str, body: EnablerPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_or_404(db, Enabler, eid, "Enabler")
    require(db, user, "enabler.manage", e.project_id)
    data = body.model_dump(exclude_unset=True)
    before = audit.snapshot(e, ["planned_date", "forecast_date", "notes"])
    if "planned_date" in data and data["planned_date"] != e.planned_date and len(body.reason.strip()) < 10:
        M.err(422, "REASON_REQUIRED", "Re-baselining a planned date needs a reason (the original stays in the audit trail)")
    if e.status == "READY" and ("forecast_date" in data or "planned_date" in data):
        M.err(409, "ALREADY_READY", "The enabler is verified ready; dates are fixed")
    if "forecast_date" in data:
        e.forecast_date = M.iso(data["forecast_date"], "forecast_date")
    if "planned_date" in data:
        e.planned_date = M.iso(data["planned_date"], "planned_date")
    if "notes" in data:
        e.notes = data["notes"]
    audit.record(db, user.id, "enabler.updated", "Enabler", e.id, project_id=e.project_id, before=before,
                 after=audit.snapshot(e, list(before)), reason=body.reason or None)
    db.commit()
    return enabler_out(db, e, _names(db))


class LinkIn(BaseModel):
    subcontract_id: str
    need_by_date: str
    purpose: str = ""


@router.post("/enablers/{eid}/links", status_code=201)
def link_subcontract(eid: str, body: LinkIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_or_404(db, Enabler, eid, "Enabler")
    require(db, user, "enabler.manage", e.project_id)
    s = get_or_404(db, Subcontract, body.subcontract_id, "Subcontract")
    if s.project_id != e.project_id:
        M.err(422, "SUBCONTRACT_NOT_IN_PROJECT", "Subcontract belongs to another project")
    link = db.scalar(select(EnablerLink).where(EnablerLink.enabler_id == eid, EnablerLink.subcontract_id == s.id))
    before = {"need_by": link.need_by_date} if link else None
    if not link:
        link = EnablerLink(enabler_id=eid, subcontract_id=s.id, need_by_date="")
        db.add(link)
    link.need_by_date, link.purpose = M.iso(body.need_by_date, "need_by_date"), body.purpose
    audit.record(db, user.id, "enabler.linked", "Enabler", e.id, project_id=e.project_id, before=before,
                 after={"subcontract": s.subcontract_number, "need_by": link.need_by_date})
    db.commit()
    return enabler_out(db, e, _names(db))


class ReadyIn(BaseModel):
    actual_date: str
    document_revision_id: str


@router.post("/enablers/{eid}/ready")
def claim_ready(eid: str, body: ReadyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_or_404(db, Enabler, eid, "Enabler")
    require(db, user, "enabler.manage", e.project_id)
    if e.status != "PLANNED":
        M.err(409, "NOT_PLANNED", f"Enabler is {e.status}")
    if body.actual_date > date.today().isoformat():
        M.err(422, "FUTURE_DATE", "Readiness cannot be claimed for a future date")
    _revision_in_project(db, user, body.document_revision_id, e.project_id)
    e.actual_date, e.readiness_revision_id = M.iso(body.actual_date, "actual_date"), body.document_revision_id
    e.status, e.claimed_by = "READY_CLAIMED", user.id
    audit.record(db, user.id, "enabler.ready_claimed", "Enabler", e.id, project_id=e.project_id,
                 after={"actual_date": e.actual_date, "evidence": body.document_revision_id})
    db.commit()
    return enabler_out(db, e, _names(db))


@router.post("/enablers/{eid}/verify")
def verify_ready(eid: str, body: VerifyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = get_or_404(db, Enabler, eid, "Enabler")
    require(db, user, "enabler.verify", e.project_id)
    if e.status != "READY_CLAIMED":
        M.err(409, "NOT_CLAIMED", f"Enabler is {e.status}")
    if user.id == e.claimed_by:
        M.err(403, "SEGREGATION_OF_DUTIES", "Whoever claimed readiness cannot verify it")
    if body.decision not in ("ACCEPT", "REJECT"):
        M.err(422, "INVALID_DECISION", "ACCEPT or REJECT")
    if body.decision == "ACCEPT":
        pending = M.prerequisites_ready(db, e)
        if pending:
            M.err(409, "PREREQUISITE_NOT_READY", "Prerequisites not ready: " +
                  ", ".join(f"{p.enabler_number} ({p.status})" for p in pending))
        e.status, e.verified_by, e.verified_at = "READY", user.id, now()
    else:
        if len(body.comment.strip()) < 5:
            M.err(422, "REASON_REQUIRED", "Say why it is not ready")
        e.status, e.actual_date, e.claimed_by = "PLANNED", None, None
    audit.record(db, user.id, f"enabler.{'verified' if body.decision == 'ACCEPT' else 'readiness_rejected'}",
                 "Enabler", e.id, project_id=e.project_id, after={"status": e.status}, reason=body.comment or None)
    db.commit()
    return enabler_out(db, e, _names(db))


class DelayEventIn(BaseModel):
    subcontract_id: str


@router.post("/enablers/{eid}/delay-event", status_code=201)
def delay_event(eid: str, body: DelayEventIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Turn measured lateness into a dated project event. The event records facts; causation and entitlement
    are assessed on a variation."""
    e = get_or_404(db, Enabler, eid, "Enabler")
    require(db, user, "event.record", e.project_id)
    link = db.scalar(select(EnablerLink).where(EnablerLink.enabler_id == eid,
                                               EnablerLink.subcontract_id == body.subcontract_id))
    if not link:
        M.err(422, "NOT_LINKED", "That subcontract is not linked to this enabler")
    v = M.link_view(e, link)
    if v["late_days"] <= 0:
        M.err(409, "NOT_LATE", "The enabler is not late for this subcontract")
    sub = db.get(Subcontract, link.subcontract_id)
    from app.models import ProjectEvent as PE
    n = len(list(db.scalars(select(PE.id).where(PE.project_id == e.project_id)))) + 1
    etype = "ACCESS_DELAY" if e.enabler_type in ("WORK_FRONT", "ACCESS_ROAD") else "FREE_ISSUE_DELAY"
    status = "available" if e.status == "READY" else "forecast"
    ev = PE(project_id=e.project_id, subcontract_id=sub.id, event_number=f"EV-{n:03d}", event_type=etype,
            occurred_on=link.need_by_date, location=e.location, enabler_id=e.id,
            description=(f"{e.enabler_number} {e.name}: needed by {sub.subcontract_number} on {link.need_by_date}"
                         f"{' for ' + link.purpose if link.purpose else ''}; {status} {v['available']} "
                         f"({v['late_days']} days late)."), source_revision_id=e.readiness_revision_id,
            recorded_by=user.id)
    db.add(ev)
    db.flush()
    audit.record(db, user.id, "event.recorded", "ProjectEvent", ev.id, project_id=e.project_id,
                 after={"number": ev.event_number, "enabler": e.enabler_number, "late_days": v["late_days"]})
    db.commit()
    return {"id": ev.id, "number": ev.event_number, "type": ev.event_type, "occurred_on": ev.occurred_on,
            "description": ev.description}
