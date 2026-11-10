"""Knowledge queries, portfolio dashboard and audit trail."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (AuditEvent, Document, DocumentRevision, ExtractionAssertion, FlowdownMapping, Obligation,
                        Passage, Subcontract, User)
from app.routers.obligations import ob_out
from app.security import current_user, require
from app.services import knowledge

router = APIRouter(prefix="/api/v1", tags=["knowledge, dashboard & audit"])


class QueryIn(BaseModel):
    projectId: str
    question: str
    subcontractId: str | None = None
    includeSupersededEvidence: bool = False
    draftWithModel: bool = False


@router.post("/knowledge/queries")
def ask(body: QueryIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.read", body.projectId)    # permission before retrieval (CI-07, SEC-01)
    return knowledge.query(db, user, body.projectId, body.question, body.subcontractId,
                           body.includeSupersededEvidence, synthesise=body.draftWithModel)


@router.get("/projects/{pid}/dashboard")
def dashboard(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.read", pid)
    subs = list(db.scalars(select(Subcontract).where(Subcontract.project_id == pid)))
    sub_ids = [s.id for s in subs]
    maps = list(db.scalars(select(FlowdownMapping).where(FlowdownMapping.subcontract_id.in_(sub_ids))))
    obs = [ob_out(o) for o in db.scalars(select(Obligation).where(Obligation.subcontract_id.in_(sub_ids)))]
    today = date.today()
    upcoming = [o for o in obs if o["due_at"] and not o["overdue"] and o["status"] != "CLOSED"
                and (date.fromisoformat(o["due_at"]) - today).days <= 14]
    revs = db.execute(select(DocumentRevision.ingestion_state).join(Document).where(Document.project_id == pid)).all()
    pending_assert = db.execute(select(ExtractionAssertion.id).join(Passage).join(
        DocumentRevision, Passage.revision_id == DocumentRevision.id).join(Document).where(
        Document.project_id == pid, ExtractionAssertion.validation_status == "PROPOSED")).all()
    by_status = {}
    for s in subs:
        by_status[s.status] = by_status.get(s.status, 0) + 1
    return {
        "subcontracts": {"total": len(subs), "by_status": by_status,
                         "active": sum(s.status in ("EXECUTED", "ACTIVE") for s in subs)},
        "flowdown": {"total": len(maps), "unresolved": sum(m.review_status != "APPROVED" for m in maps),
                     "unresolved_high": sum(m.review_status != "APPROVED" and m.risk_level == "HIGH" for m in maps)},
        "obligations": {"total": len(obs), "open": sum(o["status"] != "CLOSED" for o in obs),
                        "overdue": sum(o["overdue"] for o in obs), "due_14_days": len(upcoming),
                        "awaiting_evidence_review": sum(o["status"] == "EVIDENCE_SUBMITTED" for o in obs),
                        "closed": sum(o["status"] == "CLOSED" for o in obs)},
        "documents": {"revisions": len(revs), "review_required": sum(r[0] == "REVIEW_REQUIRED" for r in revs),
                      "conflicts": sum(r[0] == "CONFLICT" for r in revs),
                      "pending_assertions": len(pending_assert)},
        "variations": _variation_kpis(db, pid, subs),
        "mobilisation": _mobilisation_kpis(db, pid, subs),
        "payments": _payment_kpis(db, pid, subs),
        "overdue_obligations": [o for o in obs if o["overdue"]],
        "upcoming_obligations": upcoming,
    }


@router.get("/audit-events")
def audit_events(project_id: str, limit: int = 200, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "audit.read", project_id)
    users = {u.id: u.username for u in db.scalars(select(User))}
    rows = db.scalars(select(AuditEvent).where(AuditEvent.project_id == project_id)
                      .order_by(AuditEvent.id.desc()).limit(min(limit, 1000)))
    return [{"id": e.id, "at": e.occurred_at.isoformat(), "actor": users.get(e.actor_id, e.actor_id),
             "action": e.action, "object_type": e.object_type, "object_id": e.object_id, "before": e.before,
             "after": e.after, "reason": e.reason} for e in rows]


def _variation_kpis(db, pid, subs):
    from decimal import Decimal

    from app.models import Variation
    from app.services import variations as V
    vs = list(db.scalars(select(Variation).where(Variation.project_id == pid)))
    open_vs = [v for v in vs if v.status in V.OPEN_STATES]
    at_risk = [v for v in open_vs if V.notice_status(v)["at_risk"]]
    ests = [V.best_estimate(v)[0] or Decimal(0) for v in open_vs]
    exposure = sum((e for e in ests if e > 0), Decimal(0))
    omissions = sum((e for e in ests if e < 0), Decimal(0))
    approved = sum((v.approved_amount or Decimal(0) for v in vs if v.status == "APPROVED"), Decimal(0))
    return {"open": len(open_vs), "notice_at_risk": len(at_risk),
            "awaiting_approval": sum(v.status == "APPROVAL_PENDING" for v in vs),
            "open_exposure": str(exposure.quantize(Decimal("0.01"))),
            "open_omissions": str(omissions.quantize(Decimal("0.01"))),
            "approved_not_incorporated": str(approved.quantize(Decimal("0.01"))),
            "notice_alerts": [{"id": v.id, "number": v.variation_number, "title": v.title,
                               "subcontract_id": v.subcontract_id, **V.notice_status(v)}
                              for v in open_vs if V.notice_status(v)["at_risk"] or V.notice_status(v)["status"] == "PENDING"]}


def _mobilisation_kpis(db, pid, subs):
    from app.models import Enabler, EnablerLink
    from app.services import mobilisation as M
    mob = [{"subcontract_id": s.id, "subcontract_number": s.subcontract_number, "status": s.status,
            **{k: v for k, v in M.readiness(db, s).items() if k != "blocking"}}
           for s in subs if s.status in ("EXECUTED", "ACTIVE")]
    flags = []
    for e in db.scalars(select(Enabler).where(Enabler.project_id == pid)):
        for link in db.scalars(select(EnablerLink).where(EnablerLink.enabler_id == e.id)):
            v = M.link_view(e, link)
            if v["state"] in ("LATE", "AT_RISK") and not (v["state"] == "LATE" and e.status == "READY"
                                                         and v["available"] < (date.today() - timedelta(days=30)).isoformat()):
                sub = next((s for s in subs if s.id == link.subcontract_id), None)
                flags.append({"enabler_id": e.id, "number": e.enabler_number, "name": e.name, "status": e.status,
                              "subcontract_number": sub.subcontract_number if sub else "?", **v})
    flags.sort(key=lambda f: -f["late_days"])
    return {"subcontracts": mob, "enabler_flags": flags}


def _payment_kpis(db, pid, subs):
    from decimal import Decimal

    from app.models import Measurement, PaymentAdvice
    from app.services import payments as P
    advs = list(db.scalars(select(PaymentAdvice).where(PaymentAdvice.project_id == pid)))
    today = date.today().isoformat()
    with_acc = [a for a in advs if a.status in ("APPROVED", "PART_PAID")]
    month = today[:7]
    return {"awaiting_approval": sum(a.status == "SUBMITTED" for a in advs),
            "awaiting_approval_amount": str(sum((a.net_payable for a in advs if a.status == "SUBMITTED"), Decimal("0.00"))),
            "with_accounts": len(with_acc),
            "with_accounts_amount": str(sum((a.net_payable - a.paid_amount for a in with_acc), Decimal("0.00"))),
            "overdue": [{"id": a.id, "number": a.advice_number, "due_date": a.due_date,
                         "subcontract_number": next((s.subcontract_number for s in subs if s.id == a.subcontract_id), "?"),
                         "balance": str(a.net_payable - a.paid_amount)} for a in with_acc if a.due_date and a.due_date < today],
            "measurements_to_verify": sum(1 for s in subs for m in db.scalars(select(Measurement).where(
                Measurement.subcontract_id == s.id, Measurement.status == "SUBMITTED"))),
            "positions": [{"subcontract_number": s.subcontract_number, **P.position(db, s)}
                          for s in subs if s.status in ("EXECUTED", "ACTIVE")],
            "month": month}
