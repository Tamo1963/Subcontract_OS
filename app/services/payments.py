"""Payment advice calculation and to-date position (blueprint 5.4, PAY-01/02/03, ADR-0010).

Process (as the organisation works): commercial prepares a payment advice from verified measurements,
incorporated variations and any agreed advance; an approver who did not prepare it approves; the advice goes to
accounts; accounts pays outside the system and uploads the payment receipt.

Calculation (all fixed-point decimals, rounded half-up to the paisa at every step; rates from PaymentTerms):
    work value       = measured lines + incorporated variation lines (omissions are negative)
    gross            = work value + advance paid with this advice
    retention        = work value x retention %, limited so retention held never exceeds the cap
                       (cap % x current subcontract value)
    advance recovery = work value x recovery %, never more than the advance still outstanding
    GST              = work value x GST %
    TDS              = work value x TDS %
    net payable      = gross + GST - retention - advance recovery - TDS - other deductions
The to-date figures used (retention already held, advance outstanding) are frozen into the advice so the
certificate can be recomputed later and give the same answer.
"""
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Measurement, MeasurementLine, PaymentAdvice, PaymentAdviceLine, PaymentTerms, Subcontract

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
LIVE = ("DRAFT", "SUBMITTED", "APPROVED", "PART_PAID", "PAID")          # advices that count (not CANCELLED)
APPROVED_STATES = ("APPROVED", "PART_PAID", "PAID")
MODES = ["NEFT", "RTGS", "IMPS", "CHEQUE", "OTHER"]
DEDUCTION_KINDS = ["BACK_CHARGE", "DEBIT_NOTE", "MATERIAL_ISSUED", "PENALTY", "OTHER"]


def err(status, code, message, **extra):
    raise HTTPException(status, {"code": code, "message": message, **extra})


def q(x) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def dec(v, field="amount") -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        err(422, "INVALID_AMOUNT", f"{field} must be a number")


def terms_snapshot(t: PaymentTerms) -> dict:
    return {"retention_percent": str(t.retention_percent), "retention_cap_percent":
            None if t.retention_cap_percent is None else str(t.retention_cap_percent),
            "advance_amount": str(t.advance_amount), "advance_recovery_percent": str(t.advance_recovery_percent),
            "gst_percent": str(t.gst_percent), "tds_percent": str(t.tds_percent),
            "payment_due_days": t.payment_due_days, "source_reference": t.source_reference, "version": t.version}


def to_date(db: Session, sub: Subcontract, exclude_id: str | None = None) -> dict:
    """Figures from earlier live advices (cancelled ones never count)."""
    advs = [a for a in db.scalars(select(PaymentAdvice).where(PaymentAdvice.subcontract_id == sub.id,
                                                              PaymentAdvice.status.in_(LIVE)))
            if a.id != exclude_id]
    return {"current_value": str(q(sub.contract_value)),
            "retention_held": str(sum((a.retention_amount for a in advs), ZERO)),
            "advance_advised": str(sum((a.advance_amount for a in advs), ZERO)),
            "advance_recovered": str(sum((a.advance_recovery for a in advs), ZERO)),
            "advices_counted": [a.advice_number for a in advs]}


def compute(work_lines: list[Decimal], advance: Decimal, deductions: list[Decimal], terms: dict, td: dict) -> dict:
    pct = lambda k: Decimal(terms[k] or "0") / 100  # noqa: E731
    shown = lambda k: f"{Decimal(terms[k] or '0').normalize():f}"  # noqa: E731
    work = q(sum(work_lines, ZERO))
    advance = q(advance)
    gross = q(work + advance)
    retention = q(max(work, ZERO) * pct("retention_percent"))
    cap_note = "no cap"
    if terms.get("retention_cap_percent") is not None:
        cap_total = q(Decimal(td["current_value"]) * pct("retention_cap_percent"))
        room = max(cap_total - Decimal(td["retention_held"]), ZERO)
        if retention > room:
            retention = q(room)
        cap_note = f"cap {shown('retention_cap_percent')}% of {td['current_value']} = {cap_total}; held before {td['retention_held']}"
    outstanding = Decimal(td["advance_advised"]) + advance - Decimal(td["advance_recovered"])
    recovery = q(min(max(work, ZERO) * pct("advance_recovery_percent"), max(outstanding, ZERO)))
    gst = q(work * pct("gst_percent"))
    tds = q(work * pct("tds_percent"))
    other = q(sum(deductions, ZERO))
    net = q(gross + gst - retention - recovery - tds - other)
    steps = [
        {"label": "Work value (measured + variations)", "formula": " + ".join(str(q(x)) for x in work_lines) or "0", "amount": str(work)},
        {"label": "Advance paid with this advice", "formula": "per payment terms", "amount": str(advance)},
        {"label": "Gross", "formula": "work value + advance", "amount": str(gross)},
        {"label": "Retention", "formula": f"{work} x {shown('retention_percent')}% ({cap_note})", "amount": str(-retention)},
        {"label": "Advance recovery", "formula": f"{work} x {shown('advance_recovery_percent')}%, max outstanding {q(max(outstanding, ZERO))}", "amount": str(-recovery)},
        {"label": "GST", "formula": f"{work} x {shown('gst_percent')}%", "amount": str(gst)},
        {"label": "TDS", "formula": f"{work} x {shown('tds_percent')}%", "amount": str(-tds)},
        {"label": "Other deductions", "formula": " + ".join(str(q(x)) for x in deductions) or "none", "amount": str(-other)},
        {"label": "Net payable", "formula": "gross + GST - retention - recovery - TDS - deductions", "amount": str(net)},
    ]
    return {"work_value": work, "advance_amount": advance, "gross_amount": gross, "retention_amount": retention,
            "advance_recovery": recovery, "gst_amount": gst, "tds_amount": tds, "other_deductions": other,
            "net_payable": net, "steps": steps, "terms": terms, "to_date": td}


def recompute(advice: PaymentAdvice) -> dict:
    """PAY-01: recalculate from the advice's own lines, frozen terms and frozen to-date figures."""
    live = [l for l in advice.lines]
    work = [l.amount for l in live if l.source_type in ("MEASUREMENT", "VARIATION")]
    adv = sum((l.amount for l in live if l.source_type == "ADVANCE"), ZERO)
    ded = [l.amount for l in live if l.source_type == "DEDUCTION"]
    return compute(work, adv, ded, advice.terms_snapshot, advice.calculation["to_date"])


def apply(advice: PaymentAdvice, c: dict) -> None:
    for k in ("work_value", "advance_amount", "gross_amount", "retention_amount", "advance_recovery", "gst_amount",
              "tds_amount", "other_deductions", "net_payable"):
        setattr(advice, k, c[k])
    advice.calculation = {"steps": c["steps"], "to_date": c["to_date"]}


def advised_sources(db: Session, sub_id: str, source_type: str) -> set[str]:
    """Measurement lines / variations already on a live advice (cannot be paid twice)."""
    return {l.source_id for l in db.scalars(
        select(PaymentAdviceLine).join(PaymentAdvice).where(PaymentAdvice.subcontract_id == sub_id,
                                                            PaymentAdviceLine.source_type == source_type,
                                                            PaymentAdviceLine.active.is_(True)))}


def position(db: Session, sub: Subcontract) -> dict:
    advs = list(db.scalars(select(PaymentAdvice).where(PaymentAdvice.subcontract_id == sub.id)))
    ok = [a for a in advs if a.status in APPROVED_STATES]
    verified = sum((l.amount for m in db.scalars(select(Measurement).where(Measurement.subcontract_id == sub.id,
                                                                            Measurement.status == "VERIFIED"))
                    for l in m.lines), ZERO)
    t = db.get(PaymentTerms, sub.id)
    s = lambda x: str(q(x))  # noqa: E731
    return {"currency": sub.currency, "terms_set": bool(t),
            "measured_verified": s(verified),
            "work_certified": s(sum((a.work_value for a in ok), ZERO)),
            "advance_paid": s(sum((a.advance_amount for a in ok), ZERO)),
            "advance_recovered": s(sum((a.advance_recovery for a in ok), ZERO)),
            "retention_held": s(sum((a.retention_amount for a in ok), ZERO)),
            "net_approved": s(sum((a.net_payable for a in ok), ZERO)),
            "paid": s(sum((a.paid_amount for a in ok), ZERO)),
            "with_accounts": s(sum((a.net_payable - a.paid_amount for a in ok), ZERO)),
            "awaiting_approval": s(sum((a.net_payable for a in advs if a.status == "SUBMITTED"), ZERO)),
            "certified_percent_of_value": float(round(sum((a.work_value for a in ok), ZERO) * 100 / sub.contract_value, 1))
            if sub.contract_value else 0.0}


def measurement_lines_unadvised(db: Session, m: Measurement) -> list[MeasurementLine]:
    taken = advised_sources(db, m.subcontract_id, "MEASUREMENT")
    return [l for l in m.lines if l.id not in taken]
