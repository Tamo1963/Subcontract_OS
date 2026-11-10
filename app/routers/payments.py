"""Measurement, payment advice and accounts' payment receipts (blueprint 5.4; PAY-01/02/03; ADR-0010)."""
import html
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.models import (Document, DocumentRevision, Measurement, MeasurementLine, PaymentAdvice, PaymentAdviceLine,
                        PaymentRecord, PaymentTerms, Project, Subcontract, User, Variation, now)
from app.routers.common import get_or_404, load_subcontract
from app.security import PERMISSIONS, accessible_project_ids, can_see_classification, current_user, project_roles, require
from app.services import ingestion
from app.services import payments as P

router = APIRouter(prefix="/api/v1", tags=["payments"])
D = lambda x: None if x is None else str(x)  # noqa: E731


def _names(db):
    return {u.id: u.username for u in db.scalars(select(User))}


def _doc(db, rid):
    if not rid:
        return None
    r = db.get(DocumentRevision, rid)
    d = db.get(Document, r.document_id) if r else None
    return {"revision_id": rid, "document_number": d.document_number if d else "?",
            "revision": r.revision_label if r else "?", "title": d.title if d else ""}


def _rev_in_project(db, user, rid, pid):
    r = db.get(DocumentRevision, rid)
    d = db.get(Document, r.document_id) if r else None
    if not r or d.project_id != pid:
        P.err(422, "DOCUMENT_NOT_IN_PROJECT", "That document revision does not belong to this project")
    if not can_see_classification(user, d.security_classification):
        P.err(403, "CLASSIFICATION", "Document classification exceeds your clearance")


def _roles(db, user, pid):
    return project_roles(db, user, pid) or set()


def iso(d, field):
    try:
        return date.fromisoformat(d).isoformat()
    except (TypeError, ValueError):
        P.err(422, "INVALID_DATE", f"{field} must be YYYY-MM-DD")


def indian(x: Decimal) -> str:
    """12,34,56,789.00 grouping (lakh / crore), matching the web workspace."""
    sign, x = ("-" if x < 0 else ""), abs(P.q(x))
    whole, frac = f"{x:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return sign + ",".join(([head] if head else []) + groups + [tail]) + "." + frac


# ---------------------------------------------------------------- payment terms
def terms_out(t: PaymentTerms | None, names) -> dict | None:
    if not t:
        return None
    return {**P.terms_snapshot(t), "set_by": names.get(t.set_by), "set_at": t.set_at.isoformat()}


class TermsIn(BaseModel):
    retention_percent: str = "0"
    retention_cap_percent: str | None = None
    advance_amount: str = "0"
    advance_recovery_percent: str = "0"
    gst_percent: str = "0"
    tds_percent: str = "0"
    payment_due_days: int = Field(default=30, ge=0, le=365)
    source_reference: str = Field(min_length=3)
    reason: str = ""


@router.put("/subcontracts/{sid}/payment-terms")
def set_terms(sid: str, body: TermsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Rates come from the executed subcontract (cite the clause). Once an advice is approved, changing them needs a
    reason; advices already prepared keep the terms they were calculated with."""
    sub = load_subcontract(db, user, sid, "payment.terms")
    vals = {}
    for k in ("retention_percent", "advance_recovery_percent", "gst_percent", "tds_percent", "retention_cap_percent"):
        v = getattr(body, k)
        if v is None:
            vals[k] = None
            continue
        x = P.dec(v, k)
        if not Decimal(0) <= x <= Decimal(100):
            P.err(422, "INVALID_PERCENT", f"{k} must be between 0 and 100")
        vals[k] = x
    adv = P.dec(body.advance_amount, "advance_amount")
    if adv < 0 or adv > sub.contract_value:
        P.err(422, "INVALID_ADVANCE", "Advance must be between 0 and the subcontract value")
    t = db.get(PaymentTerms, sid)
    approved = db.scalar(select(PaymentAdvice.id).where(PaymentAdvice.subcontract_id == sid,
                                                       PaymentAdvice.status.in_(P.APPROVED_STATES)))
    if t and approved and len(body.reason.strip()) < 10:
        P.err(422, "REASON_REQUIRED", "Payments have been approved under the current terms; give the reason "
              "(e.g. the amendment) for changing them")
    before = P.terms_snapshot(t) if t else None
    if not t:
        t = PaymentTerms(subcontract_id=sid, set_by=user.id, version=0)
        db.add(t)
    for k, v in vals.items():
        setattr(t, k, v if v is not None else (None if k == "retention_cap_percent" else Decimal(0)))
    t.advance_amount, t.payment_due_days, t.source_reference = adv, body.payment_due_days, body.source_reference
    t.version, t.set_by, t.set_at = (t.version or 0) + 1, user.id, now()
    db.flush()
    audit.record(db, user.id, "payment.terms_set", "Subcontract", sid, project_id=sub.project_id, before=before,
                 after=P.terms_snapshot(t), reason=body.reason or None)
    db.commit()
    return terms_out(t, _names(db))


# ---------------------------------------------------------------- measurements
def measurement_out(db, m: Measurement, names, taken=None) -> dict:
    taken = taken if taken is not None else P.advised_sources(db, m.subcontract_id, "MEASUREMENT")
    return {"id": m.id, "number": m.mb_number, "period_from": m.period_from, "period_to": m.period_to,
            "status": m.status, "evidence": _doc(db, m.evidence_revision_id), "prepared_by": names.get(m.prepared_by),
            "verified_by": names.get(m.verified_by), "comment": m.comment,
            "total": str(sum((l.amount for l in m.lines), P.ZERO)),
            "advised": all(l.id in taken for l in m.lines) and bool(m.lines),
            "lines": [{"id": l.id, "line_no": l.line_no, "boq_ref": l.boq_ref, "description": l.description,
                       "unit": l.unit, "quantity": str(l.quantity), "rate": str(l.rate), "amount": str(l.amount),
                       "advised": l.id in taken} for l in m.lines]}


class MLineIn(BaseModel):
    boq_ref: str = Field(min_length=1)
    description: str = Field(min_length=3)
    unit: str = Field(min_length=1)
    quantity: str
    rate: str


class MeasurementIn(BaseModel):
    period_from: str
    period_to: str
    evidence_revision_id: str
    lines: list[MLineIn] = Field(min_length=1)


@router.post("/subcontracts/{sid}/measurements", status_code=201)
def create_measurement(sid: str, body: MeasurementIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = load_subcontract(db, user, sid, "measurement.prepare")
    if sub.status != "ACTIVE":
        P.err(409, "SUBCONTRACT_NOT_ACTIVE", "Work is measured once the subcontract is active")
    pf, pt = iso(body.period_from, "period_from"), iso(body.period_to, "period_to")
    if pf > pt or pt > date.today().isoformat():
        P.err(422, "INVALID_PERIOD", "Period must run forwards and cannot end in the future")
    _rev_in_project(db, user, body.evidence_revision_id, sub.project_id)
    n = len(list(db.scalars(select(Measurement.id).where(Measurement.subcontract_id == sid)))) + 1
    m = Measurement(subcontract_id=sid, mb_number=f"MB-{n:03d}", period_from=pf, period_to=pt,
                    evidence_revision_id=body.evidence_revision_id, prepared_by=user.id)
    for i, l in enumerate(body.lines, 1):
        qty, rate = P.dec(l.quantity, "quantity"), P.q(P.dec(l.rate, "rate"))
        if qty <= 0 or rate < 0:
            P.err(422, "INVALID_LINE", f"Line {i}: quantity must be positive and rate not negative")
        m.lines.append(MeasurementLine(line_no=i, boq_ref=l.boq_ref, description=l.description, unit=l.unit,
                                       quantity=qty.quantize(Decimal("0.001")), rate=rate, amount=P.q(qty * rate)))
    db.add(m)
    db.flush()
    audit.record(db, user.id, "measurement.submitted", "Measurement", m.id, project_id=sub.project_id,
                 after={"number": m.mb_number, "total": str(sum((l.amount for l in m.lines), P.ZERO))})
    db.commit()
    return measurement_out(db, m, _names(db))


class DecisionIn(BaseModel):
    decision: str
    comment: str = ""


@router.post("/measurements/{mid}/verify")
def verify_measurement(mid: str, body: DecisionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = get_or_404(db, Measurement, mid, "Measurement")
    sub = db.get(Subcontract, m.subcontract_id)
    require(db, user, "measurement.verify", sub.project_id)
    if m.status != "SUBMITTED":
        P.err(409, "NOT_SUBMITTED", f"Measurement is {m.status}")
    if user.id == m.prepared_by:
        P.err(403, "SEGREGATION_OF_DUTIES", "Whoever entered the measurement cannot verify it")
    if body.decision not in ("VERIFY", "RETURN"):
        P.err(422, "INVALID_DECISION", "VERIFY or RETURN")
    if body.decision == "RETURN" and len(body.comment.strip()) < 5:
        P.err(422, "REASON_REQUIRED", "Say what is wrong with the measurement")
    m.status = "VERIFIED" if body.decision == "VERIFY" else "RETURNED"
    m.verified_by, m.verified_at, m.comment = user.id, now(), body.comment
    audit.record(db, user.id, f"measurement.{m.status.lower()}", "Measurement", m.id, project_id=sub.project_id,
                 after={"status": m.status}, reason=body.comment or None)
    db.commit()
    return measurement_out(db, m, _names(db))


# ---------------------------------------------------------------- payment advices
def advice_out(db, a: PaymentAdvice, names, user=None, full=False) -> dict:
    sub = db.get(Subcontract, a.subcontract_id)
    out = {"id": a.id, "number": a.advice_number, "subcontract_id": a.subcontract_id,
           "subcontract_number": sub.subcontract_number, "project_id": a.project_id, "status": a.status,
           "period_from": a.period_from, "period_to": a.period_to, "currency": a.currency,
           "work_value": D(a.work_value), "advance_amount": D(a.advance_amount), "gross_amount": D(a.gross_amount),
           "retention_amount": D(a.retention_amount), "advance_recovery": D(a.advance_recovery),
           "other_deductions": D(a.other_deductions), "gst_amount": D(a.gst_amount), "tds_amount": D(a.tds_amount),
           "net_payable": D(a.net_payable), "paid_amount": D(a.paid_amount),
           "balance": D(a.net_payable - a.paid_amount) if a.status in P.APPROVED_STATES else None,
           "prepared_by": names.get(a.prepared_by), "approved_by": names.get(a.approved_by),
           "approved_at": a.approved_at.isoformat() if a.approved_at else None, "due_date": a.due_date,
           "overdue": a.status in ("APPROVED", "PART_PAID") and bool(a.due_date) and a.due_date < date.today().isoformat(),
           "decision_comment": a.decision_comment}
    if full:
        out.update({
            "lines": [{"line_no": l.line_no, "source_type": l.source_type, "source_id": l.source_id,
                       "description": l.description, "amount": D(l.amount), "evidence": _doc(db, l.evidence_revision_id)}
                      for l in a.lines],
            "calculation": a.calculation, "terms": a.terms_snapshot,
            "payments": [{"id": p.id, "paid_on": p.paid_on, "amount": D(p.amount), "mode": p.mode,
                          "reference": p.payment_reference, "receipt": _doc(db, p.receipt_revision_id),
                          "note": p.note, "recorded_by": names.get(p.recorded_by)}
                         for p in db.scalars(select(PaymentRecord).where(PaymentRecord.advice_id == a.id)
                                             .order_by(PaymentRecord.recorded_at))],
            "history": [{"at": e.occurred_at.isoformat(), "actor": names.get(e.actor_id, e.actor_id), "action": e.action,
                         "reason": e.reason} for e in audit_for(db, a.id)]})
    if user is not None:
        roles = _roles(db, user, a.project_id)
        acts = []
        if a.status == "DRAFT" and roles & PERMISSIONS["payment.prepare"]:
            acts += ["submit", "cancel"]
        if a.status == "SUBMITTED" and roles & PERMISSIONS["payment.approve"] and user.id != a.prepared_by:
            acts += ["approve", "return"]
        if a.status in ("APPROVED", "PART_PAID") and roles & PERMISSIONS["payment.record"] \
                and user.id not in (a.prepared_by, a.approved_by):
            acts.append("record_payment")
        out["actions"] = acts
    return out


def audit_for(db, oid):
    from app.models import AuditEvent
    return db.scalars(select(AuditEvent).where(AuditEvent.object_id == oid).order_by(AuditEvent.id))


@router.get("/subcontracts/{sid}/payments")
def payments_overview(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = load_subcontract(db, user, sid, "project.read")
    names, roles = _names(db), _roles(db, user, sub.project_id)
    taken = P.advised_sources(db, sid, "MEASUREMENT")
    vtaken = P.advised_sources(db, sid, "VARIATION")
    ms = [measurement_out(db, m, names, taken) for m in
          db.scalars(select(Measurement).where(Measurement.subcontract_id == sid).order_by(Measurement.mb_number))]
    for m, raw in zip(ms, db.scalars(select(Measurement).where(Measurement.subcontract_id == sid)
                                     .order_by(Measurement.mb_number))):
        m["can_verify"] = (raw.status == "SUBMITTED" and bool(roles & PERMISSIONS["measurement.verify"])
                           and user.id != raw.prepared_by)
    advs = [advice_out(db, a, names, user) for a in
            db.scalars(select(PaymentAdvice).where(PaymentAdvice.subcontract_id == sid).order_by(PaymentAdvice.advice_number))]
    payable_vars = [{"id": v.id, "number": v.variation_number, "title": v.title, "approved_amount": D(v.approved_amount)}
                    for v in db.scalars(select(Variation).where(Variation.subcontract_id == sid,
                                                                Variation.status.in_(("INCORPORATED", "CLOSED")),
                                                                Variation.amendment_id.is_not(None)))
                    if v.id not in vtaken]
    t = db.get(PaymentTerms, sid)
    td = P.to_date(db, sub)
    return {"subcontract_id": sid, "subcontract_number": sub.subcontract_number, "status": sub.status,
            "currency": sub.currency, "contract_value": D(sub.contract_value),
            "terms": terms_out(t, names), "position": P.position(db, sub), "measurements": ms, "advices": advs,
            "payable": {"measurements": [m for m in ms if m["status"] == "VERIFIED" and not m["advised"]],
                        "variations": payable_vars,
                        "advance_available": D(max((t.advance_amount if t else P.ZERO) - Decimal(td["advance_advised"]), P.ZERO))},
            "can": {"terms": bool(roles & PERMISSIONS["payment.terms"]),
                    "measure": bool(roles & PERMISSIONS["measurement.prepare"]) and sub.status == "ACTIVE",
                    "prepare": bool(roles & PERMISSIONS["payment.prepare"]) and bool(t)},
            "deduction_kinds": P.DEDUCTION_KINDS, "modes": P.MODES}


class DeductionIn(BaseModel):
    kind: str
    description: str = Field(min_length=5)
    amount: str
    evidence_revision_id: str | None = None


class AdviceIn(BaseModel):
    period_from: str | None = None
    period_to: str | None = None
    measurement_ids: list[str] = []
    variation_ids: list[str] = []
    advance_amount: str = "0"
    deductions: list[DeductionIn] = []


@router.post("/subcontracts/{sid}/payment-advices", status_code=201)
def create_advice(sid: str, body: AdviceIn, user: User = Depends(current_user), db: Session = Depends(get_db),
                  idempotency_key: str | None = Header(default=None)):
    sub = load_subcontract(db, user, sid, "payment.prepare")
    payload = {"sid": sid, **body.model_dump()}
    replay = audit.idempotent_lookup(db, idempotency_key, user.id, payload)        # PAY-02
    if replay:
        return replay
    if sub.status not in ("EXECUTED", "ACTIVE"):
        P.err(409, "SUBCONTRACT_NOT_EXECUTED", "Payments are advised on an executed subcontract")
    terms = db.get(PaymentTerms, sid)
    if not terms:
        P.err(409, "TERMS_NOT_SET", "Set the payment terms (retention, advance, GST, TDS) from the subcontract first")
    lines, n = [], 0
    taken = P.advised_sources(db, sid, "MEASUREMENT")
    for mid in dict.fromkeys(body.measurement_ids):
        m = get_or_404(db, Measurement, mid, "Measurement")
        if m.subcontract_id != sid:
            P.err(422, "NOT_THIS_SUBCONTRACT", f"{m.mb_number} belongs to another subcontract")
        if m.status != "VERIFIED":
            P.err(409, "MEASUREMENT_NOT_VERIFIED", f"{m.mb_number} is {m.status}; only verified measurements are paid")
        fresh = [l for l in m.lines if l.id not in taken]
        if not fresh:
            P.err(409, "ALREADY_ADVISED", f"{m.mb_number} is already on another payment advice")
        for l in fresh:
            n += 1
            lines.append(PaymentAdviceLine(line_no=n, source_type="MEASUREMENT", source_id=l.id, amount=l.amount,
                                           evidence_revision_id=m.evidence_revision_id,
                                           description=f"{m.mb_number} · {l.boq_ref} {l.description}: {l.quantity} {l.unit} x {l.rate}"))
    vtaken = P.advised_sources(db, sid, "VARIATION")
    for vid in dict.fromkeys(body.variation_ids):
        v = get_or_404(db, Variation, vid, "Variation")
        if v.subcontract_id != sid:
            P.err(422, "NOT_THIS_SUBCONTRACT", f"{v.variation_number} belongs to another subcontract")
        if v.status not in ("INCORPORATED", "CLOSED") or not v.amendment_id:
            P.err(409, "VARIATION_NOT_INCORPORATED", f"{v.variation_number} is {v.status}; only variations incorporated "
                  "by amendment are paid")
        if v.id in vtaken:
            P.err(409, "ALREADY_ADVISED", f"{v.variation_number} is already on another payment advice")
        n += 1
        lines.append(PaymentAdviceLine(line_no=n, source_type="VARIATION", source_id=v.id, amount=v.approved_amount,
                                       description=f"{v.variation_number} {v.title} (approved)"))
    td = P.to_date(db, sub)
    advance = P.q(P.dec(body.advance_amount, "advance_amount"))
    if advance < 0 or advance > terms.advance_amount - Decimal(td["advance_advised"]):
        P.err(422, "ADVANCE_EXCEEDS_TERMS", f"Advance available under the terms: "
              f"{terms.advance_amount - Decimal(td['advance_advised'])}")
    if advance:
        n += 1
        lines.append(PaymentAdviceLine(line_no=n, source_type="ADVANCE", amount=advance,
                                       description=f"Advance payment ({terms.source_reference})"))
    ded = []
    for d in body.deductions:
        amt = P.q(P.dec(d.amount, "deduction"))
        if d.kind not in P.DEDUCTION_KINDS or amt <= 0:
            P.err(422, "INVALID_DEDUCTION", f"Deduction kind one of {P.DEDUCTION_KINDS}; amount positive")
        if d.kind in ("BACK_CHARGE", "DEBIT_NOTE", "PENALTY") and not d.evidence_revision_id:
            P.err(422, "EVIDENCE_REQUIRED", f"A {d.kind.lower().replace('_', ' ')} needs its supporting document")
        if d.evidence_revision_id:
            _rev_in_project(db, user, d.evidence_revision_id, sub.project_id)
        n += 1
        ded.append(amt)
        lines.append(PaymentAdviceLine(line_no=n, source_type="DEDUCTION", amount=amt,
                                       evidence_revision_id=d.evidence_revision_id,
                                       description=f"{d.kind.replace('_', ' ').title()}: {d.description}"))
    if not any(l.source_type in ("MEASUREMENT", "VARIATION", "ADVANCE") for l in lines):
        P.err(422, "NOTHING_TO_PAY", "Include verified measurements, incorporated variations or an advance")
    snap = P.terms_snapshot(terms)
    c = P.compute([l.amount for l in lines if l.source_type in ("MEASUREMENT", "VARIATION")], advance, ded, snap, td)
    if c["net_payable"] < 0:
        P.err(422, "NEGATIVE_NET", f"Deductions exceed the amount payable (net {c['net_payable']})")
    count = len(list(db.scalars(select(PaymentAdvice.id).where(PaymentAdvice.subcontract_id == sid))))
    a = PaymentAdvice(project_id=sub.project_id, subcontract_id=sid, advice_number=f"PA-{count + 1:03d}",
                      period_from=iso(body.period_from, "period_from") if body.period_from else None,
                      period_to=iso(body.period_to, "period_to") if body.period_to else None,
                      currency=sub.currency, terms_snapshot=snap, prepared_by=user.id, calculation={}, **{
                          k: c[k] for k in ("work_value", "advance_amount", "gross_amount", "retention_amount",
                                            "advance_recovery", "gst_amount", "tds_amount", "other_deductions",
                                            "net_payable")})
    a.calculation = {"steps": c["steps"], "to_date": c["to_date"]}
    a.lines = lines
    db.add(a)
    db.flush()
    audit.record(db, user.id, "payment.advice_prepared", "PaymentAdvice", a.id, project_id=sub.project_id,
                 after={"number": a.advice_number, "net_payable": a.net_payable, "lines": len(lines)})
    out = advice_out(db, a, _names(db), full=True)
    audit.idempotent_store(db, idempotency_key, user.id, payload, out)
    db.commit()
    return out


def _load(db, user, aid, action="project.read"):
    a = get_or_404(db, PaymentAdvice, aid, "Payment advice")
    require(db, user, action, a.project_id)
    return a


@router.get("/payment-advices/{aid}")
def get_advice(aid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = _load(db, user, aid)
    return advice_out(db, a, _names(db), user, full=True)


@router.get("/payment-advices/{aid}/recalculate")
def recalculate(aid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """PAY-01: recompute from the stored lines, frozen terms and frozen to-date figures; report any difference."""
    a = _load(db, user, aid)
    c = P.recompute(a)
    fields = ("work_value", "advance_amount", "gross_amount", "retention_amount", "advance_recovery", "gst_amount",
              "tds_amount", "other_deductions", "net_payable")
    diffs = {k: {"stored": D(getattr(a, k)), "recomputed": D(c[k])} for k in fields if getattr(a, k) != c[k]}
    return {"reconciles": not diffs, "differences": diffs, "recomputed": {k: D(c[k]) for k in fields},
            "steps": c["steps"]}


class AdviceDecisionIn(BaseModel):
    action: str             # SUBMIT | APPROVE | RETURN | CANCEL
    comment: str = ""


@router.post("/payment-advices/{aid}/decision")
def decide(aid: str, body: AdviceDecisionIn, user: User = Depends(current_user), db: Session = Depends(get_db),
           idempotency_key: str | None = Header(default=None)):
    a = _load(db, user, aid)
    payload = {"aid": aid, **body.model_dump()}
    replay = audit.idempotent_lookup(db, idempotency_key, user.id, payload)
    if replay:
        return replay
    before = a.status
    if body.action == "SUBMIT":
        require(db, user, "payment.prepare", a.project_id)
        if a.status != "DRAFT":
            P.err(409, "INVALID_TRANSITION", f"Advice is {a.status}")
        a.status, a.submitted_at = "SUBMITTED", now()
    elif body.action in ("APPROVE", "RETURN"):
        require(db, user, "payment.approve", a.project_id)
        if a.status != "SUBMITTED":
            P.err(409, "INVALID_TRANSITION", f"Advice is {a.status}")
        if user.id == a.prepared_by:                                                       # PAY-03
            P.err(403, "SEGREGATION_OF_DUTIES", "Whoever prepared the payment advice cannot approve it")
        if body.action == "RETURN":
            if len(body.comment.strip()) < 5:
                P.err(422, "REASON_REQUIRED", "Say what needs correcting")
            a.status = "DRAFT"
        else:
            c = P.recompute(a)                    # never approve figures that do not reconcile
            if c["net_payable"] != a.net_payable:
                P.err(409, "DOES_NOT_RECONCILE", "Stored figures do not match a recalculation; recheck the advice")
            a.status, a.approved_by, a.approved_at = "APPROVED", user.id, now()
            a.due_date = (date.today() + timedelta(days=int(a.terms_snapshot.get("payment_due_days", 30)))).isoformat()
        a.decision_comment = body.comment
    elif body.action == "CANCEL":
        require(db, user, "payment.prepare", a.project_id)
        if a.status != "DRAFT":
            P.err(409, "INVALID_TRANSITION", "Only a draft advice can be cancelled")
        if len(body.comment.strip()) < 5:
            P.err(422, "REASON_REQUIRED", "Give a reason")
        a.status = "CANCELLED"
        for l in a.lines:
            l.active = False            # measurements / variations become payable again
        a.decision_comment = body.comment
    else:
        P.err(422, "INVALID_ACTION", "SUBMIT, APPROVE, RETURN or CANCEL")
    audit.record(db, user.id, f"payment.advice_{body.action.lower()}", "PaymentAdvice", a.id, project_id=a.project_id,
                 before={"status": before}, after={"status": a.status, "net_payable": a.net_payable},
                 reason=body.comment or None)
    if a.status == "APPROVED":
        audit.record(db, user.id, "payment.sent_to_accounts", "PaymentAdvice", a.id, project_id=a.project_id,
                     after={"net_payable": a.net_payable, "due_date": a.due_date})
    db.commit()
    out = advice_out(db, a, _names(db), user, full=True)
    audit.idempotent_store(db, idempotency_key, user.id, payload, out)
    db.commit()
    return out


@router.post("/payment-advices/{aid}/payments", status_code=201)
async def record_payment(aid: str, receipt: UploadFile = File(...), paid_on: str = Form(...), amount: str = Form(...),
                         mode: str = Form(...), payment_reference: str = Form(...), note: str = Form(""),
                         user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Accounts paid outside the system: record the payment and upload the bank receipt / advice."""
    a = _load(db, user, aid, "payment.record")
    if a.status not in ("APPROVED", "PART_PAID"):
        P.err(409, "NOT_WITH_ACCOUNTS", f"Advice is {a.status}; only approved advices are paid")
    if user.id in (a.prepared_by, a.approved_by):
        P.err(403, "SEGREGATION_OF_DUTIES", "Whoever prepared or approved the advice cannot record its payment")
    paid_on = iso(paid_on, "paid_on")
    if paid_on > date.today().isoformat():
        P.err(422, "FUTURE_DATE", "Payment date cannot be in the future")
    if paid_on < a.approved_at.date().isoformat():
        P.err(422, "BEFORE_APPROVAL", f"Payment date is before the advice was approved ({a.approved_at.date()})")
    amt = P.q(P.dec(amount, "amount"))
    balance = a.net_payable - a.paid_amount
    if amt <= 0 or amt > balance:
        P.err(422, "AMOUNT_EXCEEDS_BALANCE", f"Amount must be positive and not more than the balance {balance}")
    if mode not in P.MODES:
        P.err(422, "INVALID_MODE", f"Mode one of {P.MODES}")
    ref = payment_reference.strip()
    if len(ref) < 4:
        P.err(422, "REFERENCE_REQUIRED", "Enter the bank / UTR / cheque reference")
    if db.scalar(select(PaymentRecord.id).where(PaymentRecord.subcontract_id == a.subcontract_id,
                                                PaymentRecord.payment_reference == ref)):
        P.err(409, "DUPLICATE_REFERENCE", "That payment reference is already recorded for this subcontract")
    sub = db.get(Subcontract, a.subcontract_id)
    data = await receipt.read()
    k = len(list(db.scalars(select(PaymentRecord.id).where(PaymentRecord.advice_id == a.id)))) + 1
    res = ingestion.register_revision(
        db, actor_id=user.id, project_id=a.project_id, document_number=f"RCPT-{sub.subcontract_number}-{a.advice_number}-{k}",
        revision_label="0", document_type="PAYMENT_RECEIPT", title=f"Payment receipt {ref} for {a.advice_number}",
        filename=receipt.filename or "receipt", data=data, subcontract_id=sub.id, source_party="EPC_CONTRACTOR",
        security_classification="CONFIDENTIAL", issued_at=paid_on, allow_images=True)
    if res.outcome != "NEW_DOCUMENT":
        P.err(409, "RECEIPT_ALREADY_USED", res.message)
    rec = PaymentRecord(advice_id=a.id, subcontract_id=a.subcontract_id, paid_on=paid_on, amount=amt, mode=mode,
                        payment_reference=ref, receipt_revision_id=res.revision.id, note=note, recorded_by=user.id)
    db.add(rec)
    a.paid_amount = a.paid_amount + amt
    a.status = "PAID" if a.paid_amount == a.net_payable else "PART_PAID"
    db.flush()
    audit.record(db, user.id, "payment.paid", "PaymentAdvice", a.id, project_id=a.project_id,
                 after={"amount": amt, "reference": ref, "paid_on": paid_on, "status": a.status,
                        "receipt_revision_id": res.revision.id})
    db.commit()
    return advice_out(db, a, _names(db), user, full=True)


@router.get("/payment-advices")
def accounts_queue(status: str = "WITH_ACCOUNTS", user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Accounts' queue across the projects the user can see."""
    pids = [p for p in accessible_project_ids(db, user) if _roles(db, user, p) & PERMISSIONS["payment.read_queue"]]
    states = ("APPROVED", "PART_PAID") if status == "WITH_ACCOUNTS" else (status,)
    names = _names(db)
    rows = [advice_out(db, a, names, user) for a in db.scalars(
        select(PaymentAdvice).where(PaymentAdvice.project_id.in_(pids), PaymentAdvice.status.in_(states))
        .order_by(PaymentAdvice.due_date, PaymentAdvice.advice_number))]
    return {"advices": rows, "total_balance": str(sum((Decimal(r["balance"] or "0") for r in rows), P.ZERO))}


@router.get("/payment-advices/{aid}/print", response_class=HTMLResponse)
def print_advice(aid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Printable payment advice for accounts (only once approved)."""
    a = _load(db, user, aid)
    if a.status not in P.APPROVED_STATES:
        P.err(409, "NOT_APPROVED", "Only an approved advice is printed for accounts")
    sub, names = db.get(Subcontract, a.subcontract_id), _names(db)
    proj = db.get(Project, a.project_id)
    from app.models import Subcontractor
    sc = db.get(Subcontractor, sub.subcontractor_id)
    e = html.escape
    money = lambda x: indian(Decimal(x)) if a.currency == "INR" else f"{Decimal(x):,.2f}"  # noqa: E731
    rows = "".join(f"<tr><td>{l.line_no}</td><td>{e(l.source_type.title())}</td><td>{e(l.description)}</td>"
                   f"<td class=r>{money(l.amount)}</td></tr>" for l in a.lines)
    steps = "".join(f"<tr><td>{e(s['label'])}</td><td class=f>{e(s['formula'])}</td><td class=r>{money(s['amount'])}</td></tr>"
                    for s in a.calculation["steps"] if s["label"] != "Net payable")
    return f"""<!doctype html><html><head><meta charset=utf-8><title>{e(a.advice_number)} {e(sub.subcontract_number)}</title>
<style>body{{font:13px system-ui,Segoe UI,sans-serif;color:#111;max-width:860px;margin:28px auto;padding:0 16px}}
h1{{font-size:20px;margin:0}} table{{width:100%;border-collapse:collapse;margin:12px 0}} td,th{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}
.r{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}} .f{{color:#555;font-size:12px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:4px 24px}}
.net td{{font-weight:700;font-size:15px;border-top:2px solid #111}} .muted{{color:#666}} @media print{{button{{display:none}}}}</style></head><body>
<button onclick="print()">Print</button>
<h1>Payment advice {e(a.advice_number)}</h1><p class=muted>{e(proj.project_code)} · {e(proj.name)}</p>
<div class=grid><div><b>Payee</b><br>{e(sc.legal_name)}</div><div><b>Subcontract</b><br>{e(sub.subcontract_number)} — {e(sub.scope_summary)}</div>
<div><b>Period</b><br>{e(a.period_from or '—')} to {e(a.period_to or '—')}</div><div><b>Pay by</b><br>{e(a.due_date or '')}</div>
<div><b>Prepared by</b><br>{e(names.get(a.prepared_by, ''))}</div><div><b>Approved by</b><br>{e(names.get(a.approved_by, ''))} on {e((a.approved_at.isoformat() if a.approved_at else '')[:10])}</div></div>
<table><tr><th>#</th><th>Type</th><th>Description</th><th class=r>Amount ({e(a.currency)})</th></tr>{rows}</table>
<table><tr><th>Calculation</th><th>Basis</th><th class=r>Amount</th></tr>{steps}
<tr class=net><td colspan=2>Net payable</td><td class=r>{money(a.net_payable)}</td></tr></table>
<p class=muted>Terms version {a.terms_snapshot.get('version')} ({e(a.terms_snapshot.get('source_reference', ''))}). Paid to date {money(a.paid_amount)}.
Accounts: pay outside Subcontract OS, then record the payment and upload the bank receipt against {e(a.advice_number)}.</p></body></html>"""


@router.get("/payment-advices/{aid}/trace")
def trace(aid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Knowledge-graph view: advice VALUES measured work / variations; APPROVED_BY; SETTLED_BY receipts."""
    a = _load(db, user, aid)
    names = _names(db)
    nodes, edges = [], []
    node = lambda kind, nid, label: nodes.append({"kind": kind, "id": nid, "label": label})  # noqa: E731
    node("PaymentAdvice", a.id, f"{a.advice_number} net {a.net_payable} ({a.status})")
    seen = set()
    for l in a.lines:
        if l.source_type == "MEASUREMENT":
            ml = db.get(MeasurementLine, l.source_id)
            m = ml.measurement
            if m.id not in seen:
                seen.add(m.id)
                node("Measurement", m.id, f"{m.mb_number} {m.period_from}..{m.period_to} verified by {names.get(m.verified_by)}")
                edges.append((a.id, "VALUES", m.id))
                ev = _doc(db, m.evidence_revision_id)
                node("Evidence", f"ev-{m.id}", f"{ev['document_number']} rev {ev['revision']}")
                edges.append((m.id, "EVIDENCED_BY", f"ev-{m.id}"))
        elif l.source_type == "VARIATION":
            v = db.get(Variation, l.source_id)
            node("Variation", v.id, f"{v.variation_number} {v.title} approved {v.approved_amount}")
            edges.append((a.id, "VALUES", v.id))
        elif l.source_type == "DEDUCTION":
            node("Deduction", l.id, f"{l.description} {l.amount}")
            edges.append((a.id, "DEDUCTS", l.id))
    if a.approved_by:
        node("Approval", f"appr-{a.id}", f"Approved by {names.get(a.approved_by)} → sent to accounts")
        edges.append((a.id, "APPROVED_BY", f"appr-{a.id}"))
    for p in db.scalars(select(PaymentRecord).where(PaymentRecord.advice_id == a.id)):
        r = _doc(db, p.receipt_revision_id)
        node("Payment", p.id, f"{p.mode} {p.payment_reference} {p.amount} on {p.paid_on} (receipt {r['document_number']})")
        edges.append((a.id, "SETTLED_BY", p.id))
    return {"nodes": nodes, "edges": [{"from": f, "type": t, "to": to} for f, t, to in edges]}
