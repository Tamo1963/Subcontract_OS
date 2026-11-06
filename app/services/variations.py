"""Variation workflow (blueprint Section 5.3, ADR-0008).

IDENTIFIED -> CLASSIFIED -> NOTICE_REVIEW -> ENTITLEMENT_ASSESSMENT -> COST_AND_TIME_ASSESSMENT
  -> COMMERCIAL_REVIEW -> APPROVAL_PENDING -> APPROVED / REJECTED / WITHDRAWN -> INCORPORATED -> CLOSED

Rules that matter commercially:
- The notice clock comes from the subcontract's own flowed-down notice obligation, never a generic default.
  A late, missing or unknown notice is flagged and can only be passed by a named legal reviewer or approver
  with a recorded reason (VAR-01). Nothing is waived silently.
- Claimed, assessed, recommended and approved values are separate fields; none overwrites another.
  Item amounts are computed by the server in fixed-point decimals.
- Segregation of duties: the assessor cannot recommend; the raiser, assessor and recommender cannot approve.
- Only incorporation changes the subcontract value, through an amendment; the executed baseline is never
  edited. Approved-but-not-incorporated values enter the forecast; open variations are exposure only (VAR-02).
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import Amendment, Obligation, Subcontract, User, Variation, now
from app.services.lifecycle import LOCKED_STATES, add_days

STATES = ["IDENTIFIED", "CLASSIFIED", "NOTICE_REVIEW", "ENTITLEMENT_ASSESSMENT", "COST_AND_TIME_ASSESSMENT",
          "COMMERCIAL_REVIEW", "APPROVAL_PENDING", "APPROVED", "REJECTED", "WITHDRAWN", "INCORPORATED", "CLOSED"]
OPEN_STATES = set(STATES[:7])
EDITABLE_STATES = set(STATES[:6])
TRANSITIONS = {
    "IDENTIFIED": {"CLASSIFIED", "WITHDRAWN"},
    "CLASSIFIED": {"NOTICE_REVIEW", "IDENTIFIED", "WITHDRAWN"},
    "NOTICE_REVIEW": {"ENTITLEMENT_ASSESSMENT", "CLASSIFIED", "WITHDRAWN"},
    "ENTITLEMENT_ASSESSMENT": {"COST_AND_TIME_ASSESSMENT", "NOTICE_REVIEW", "REJECTED", "WITHDRAWN"},
    "COST_AND_TIME_ASSESSMENT": {"COMMERCIAL_REVIEW", "ENTITLEMENT_ASSESSMENT", "WITHDRAWN"},
    "COMMERCIAL_REVIEW": {"COST_AND_TIME_ASSESSMENT", "WITHDRAWN"},   # forward only through /recommend
    "APPROVAL_PENDING": set(),                                       # only through /decision
    "APPROVED": set(),                                               # only through /incorporate
    "REJECTED": {"CLOSED"},
    "INCORPORATED": {"CLOSED"},
    "WITHDRAWN": set(),
    "CLOSED": set(),
}
# Which permission moves a variation INTO each state through /transitions
TRANSITION_PERMISSION = {"CLASSIFIED": "variation.raise", "IDENTIFIED": "variation.raise",
                         "NOTICE_REVIEW": "variation.raise", "ENTITLEMENT_ASSESSMENT": "variation.assess",
                         "COST_AND_TIME_ASSESSMENT": "variation.assess", "COMMERCIAL_REVIEW": "variation.assess",
                         "REJECTED": "variation.assess", "WITHDRAWN": "variation.raise", "CLOSED": "variation.assess"}
CATEGORIES = ["SCOPE_ADDITION", "SCOPE_OMISSION", "SPECIFICATION_CHANGE", "DESIGN_CHANGE", "ACCESS_DELAY",
              "SITE_CONDITION", "ACCELERATION", "SUSPENSION", "OTHER"]
EVENT_TYPES = ["INSTRUCTION", "DESIGN_CHANGE", "ACCESS_DELAY", "SITE_CONDITION", "SUSPENSION", "WEATHER",
               "FREE_ISSUE_DELAY", "OTHER"]
ENTITLEMENT = ["NOT_ASSESSED", "ENTITLED", "PARTIALLY_ENTITLED", "NOT_ENTITLED", "DISPUTED"]
COST_TYPES = ["LABOUR", "MATERIAL", "EQUIPMENT", "SUBCONTRACT", "PRELIMINARIES", "OTHER"]
PRICING_BASES = ["CONTRACT_RATE", "STAR_RATE", "DAYWORK", "QUOTATION", "LUMP_SUM"]
EVIDENCE_KINDS = ["INSTRUCTION", "NOTICE", "QUOTATION", "PROGRAMME", "CORRESPONDENCE", "MEASUREMENT", "PHOTOGRAPH",
                  "OTHER"]
CENT = Decimal("0.01")


def err(status, code, message, **extra):
    raise HTTPException(status, {"code": code, "message": message, **extra})


def money(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        err(422, "INVALID_AMOUNT", f"Not a valid amount: {v}")


def iso(d: str | None, field: str) -> str | None:
    if not d:
        return None
    try:
        return date.fromisoformat(d).isoformat()
    except ValueError:
        err(422, "INVALID_DATE", f"{field} must be YYYY-MM-DD")


# ---------------------------------------------------------------- notice clock
def notice_source(db: Session, sub: Subcontract) -> Obligation | None:
    """The subcontract's own notice obligation: a VARIATION notice if one was flowed down, else NOTICE."""
    obs = list(db.scalars(select(Obligation).where(Obligation.subcontract_id == sub.id,
                                                   Obligation.obligation_type.in_(("VARIATION", "NOTICE")),
                                                   Obligation.notice_period_days.is_not(None))))
    obs.sort(key=lambda o: (o.obligation_type != "VARIATION", o.obligation_number))
    return obs[0] if obs else None


def set_clock(db: Session, v: Variation, sub: Subcontract) -> None:
    ob = notice_source(db, sub)
    v.notice_obligation_id = ob.id if ob else None
    v.notice_period_days = ob.notice_period_days if ob else None
    v.notice_day_basis = ob.day_basis if ob else "CALENDAR"
    v.notice_due_on = None
    if ob and v.notice_clock_start:
        v.notice_due_on = add_days(date.fromisoformat(v.notice_clock_start), ob.notice_period_days,
                                   ob.day_basis).isoformat()


def notice_status(v: Variation, today: date | None = None) -> dict:
    today = today or date.today()
    d = v.notice_risk_decision
    if d and d.get("decision") == "NOTICE_NOT_REQUIRED":
        s, msg = "NOT_REQUIRED", f"Not required — {d['reason']} ({d['by']})"
    elif v.notice_period_days is None:
        s = "NO_NOTICE_CLAUSE"
        msg = "No notice period was found in this subcontract's obligations: confirm with legal before assessing."
    elif not v.notice_clock_start:
        s, msg = "CLOCK_NOT_STARTED", "Enter the date the event occurred or became known to start the notice clock."
    elif v.notice_given_on:
        late = v.notice_given_on > v.notice_due_on
        s = "GIVEN_LATE" if late else "GIVEN_ON_TIME"
        msg = (f"Notice given {v.notice_given_on}, due {v.notice_due_on}" + (" — LATE." if late else " — on time."))
        if not v.notice_revision_id:
            s, msg = "GIVEN_NO_EVIDENCE", msg + " Attach the notice document as evidence."
    elif today.isoformat() > v.notice_due_on:
        s, msg = "MISSING_OVERDUE", f"No notice recorded; it was due {v.notice_due_on}."
    else:
        left = (date.fromisoformat(v.notice_due_on) - today).days
        s, msg = "PENDING", f"Notice due {v.notice_due_on} ({left} days left)."
    risk = s not in ("GIVEN_ON_TIME", "PENDING", "NOT_REQUIRED")
    return {"status": s, "message": msg, "at_risk": risk and not d, "risk_decision": d,
            "period_days": v.notice_period_days, "day_basis": v.notice_day_basis, "clock_start": v.notice_clock_start,
            "due_on": v.notice_due_on, "given_on": v.notice_given_on, "evidence_revision_id": v.notice_revision_id,
            "can_proceed": s in ("GIVEN_ON_TIME", "NOT_REQUIRED") or bool(d)}


# ---------------------------------------------------------------- numbering and creation
def next_number(db: Session, sub_id: str) -> str:
    n = (db.scalar(select(func.count()).select_from(Variation).where(Variation.subcontract_id == sub_id)) or 0) + 1
    return f"VAR-{n:03d}"


def require_executed(sub: Subcontract) -> None:
    if sub.status not in LOCKED_STATES or sub.status == "CLOSED_OUT":
        err(409, "SUBCONTRACT_NOT_ACTIVE", "Variations are raised against an executed, active subcontract; "
            "change a draft directly instead")


# ---------------------------------------------------------------- transitions
def gate(db: Session, v: Variation, target: str) -> None:
    """Entry conditions for each state (blueprint 5.2: explicit entry conditions and required evidence)."""
    if target == "CLASSIFIED" and v.status == "IDENTIFIED":
        if not v.category or len((v.description or "").strip()) < 10:
            err(422, "INCOMPLETE", "Classify the variation (category) and describe the change first")
    if target == "ENTITLEMENT_ASSESSMENT" and v.status == "NOTICE_REVIEW":
        n = notice_status(v)
        if not n["can_proceed"]:   # VAR-01: flagged, never silently waived
            if n["status"] == "PENDING":
                err(409, "NOTICE_NOT_YET_GIVEN", n["message"] + " Record the notice (date and document) before "
                    "assessing entitlement.", notice=n)
            err(409, "NOTICE_RISK_UNRESOLVED", n["message"] + " A legal reviewer or approver must record a "
                "notice-risk decision before entitlement is assessed.", notice=n)
    if target == "COST_AND_TIME_ASSESSMENT" and v.status == "ENTITLEMENT_ASSESSMENT":
        if v.entitlement_status not in ("ENTITLED", "PARTIALLY_ENTITLED"):
            err(409, "NO_ENTITLEMENT", "Record entitlement (entitled or partially entitled, with the contractual "
                "basis) before valuing the change; not entitled -> reject")
    if target == "REJECTED" and v.status == "ENTITLEMENT_ASSESSMENT" and v.entitlement_status != "NOT_ENTITLED":
        err(409, "ENTITLEMENT_NOT_REFUSED", "Record entitlement as not entitled (with reasons) before rejecting")
    if target == "COMMERCIAL_REVIEW":
        if v.assessed_amount is None and v.assessed_days is None:
            err(422, "NOT_VALUED", "Price the change (items) and/or assess the time impact first")


def move(db: Session, v: Variation, target: str, user: User, reason: str | None = None) -> None:
    if target not in TRANSITIONS.get(v.status, set()):
        err(409, "INVALID_TRANSITION", f"{v.status} -> {target} is not permitted here; allowed: "
            f"{sorted(TRANSITIONS.get(v.status, set()))}")
    gate(db, v, target)
    if target in ("WITHDRAWN", "REJECTED") and not (reason or "").strip():
        err(422, "REASON_REQUIRED", "Give a reason")
    before = v.status
    v.status = target
    if target in ("REJECTED", "WITHDRAWN"):
        v.decided_by, v.decided_at, v.decision_reason = user.id, now(), reason or ""
    audit.record(db, user.id, "variation.transition", "Variation", v.id, project_id=v.project_id,
                 before={"status": before}, after={"status": target}, reason=reason)


def decide(db: Session, v: Variation, user: User, decision: str, amount, days, reason: str) -> None:
    if v.status != "APPROVAL_PENDING":
        err(409, "NOT_PENDING_APPROVAL", f"Variation is {v.status}")
    if user.id in {v.created_by, v.assessed_by, v.recommended_by}:
        err(403, "SEGREGATION_OF_DUTIES", "Whoever raised, assessed or recommended a variation cannot decide it")
    if decision not in ("APPROVE", "REJECT", "RETURN"):
        err(422, "INVALID_DECISION", "APPROVE, REJECT or RETURN")
    if decision != "APPROVE" and not (reason or "").strip():
        err(422, "REASON_REQUIRED", "Give a reason")
    before = {"status": v.status}
    if decision == "RETURN":
        v.status = "COMMERCIAL_REVIEW"
        v.recommended_by = None
    elif decision == "REJECT":
        v.status, v.decided_by, v.decided_at, v.decision_reason = "REJECTED", user.id, now(), reason
    else:
        amt = money(amount) if amount is not None else v.recommended_amount
        dys = days if days is not None else v.recommended_days
        if amt is None and dys is None:
            err(422, "NOTHING_TO_APPROVE", "Approve an amount and/or days")
        if (amt != v.recommended_amount or dys != v.recommended_days) and not (reason or "").strip():
            err(422, "REASON_REQUIRED", "Approving a value different from the recommendation needs a reason")
        v.approved_amount, v.approved_days = amt, dys
        v.status, v.decided_by, v.decided_at, v.decision_reason = "APPROVED", user.id, now(), reason or ""
    audit.record(db, user.id, f"variation.{decision.lower()}d" if decision != "RETURN" else "variation.returned",
                 "Variation", v.id, project_id=v.project_id, before=before,
                 after={"status": v.status, "approved_amount": v.approved_amount, "approved_days": v.approved_days},
                 reason=reason)


def incorporate(db: Session, v: Variation, sub: Subcontract, user: User) -> Amendment | None:
    """The only path by which a variation changes the subcontract value: an amendment carrying the approval."""
    if v.status != "APPROVED":
        err(409, "NOT_APPROVED", f"Variation is {v.status}; only approved variations are incorporated")
    a = None
    if v.approved_amount:
        n = (db.scalar(select(func.count()).select_from(Amendment).where(Amendment.subcontract_id == sub.id)) or 0) + 1
        old = sub.contract_value
        new = (Decimal(old) + v.approved_amount).quantize(CENT)
        a = Amendment(subcontract_id=sub.id, amendment_number=n, field="contract_value", old_value=str(old),
                      new_value=str(new), reason=f"{v.variation_number}: {v.title} (approved {v.decided_at:%Y-%m-%d})",
                      requested_by=user.id, approved_by=v.decided_by, status="APPROVED")
        db.add(a)
        db.flush()
        sub.contract_value = new
        v.amendment_id = a.id
        audit.record(db, user.id, "amendment.approved", "Subcontract", sub.id, project_id=sub.project_id,
                     before={"contract_value": str(old)},
                     after={"contract_value": str(new), "amendment_id": a.id, "variation_id": v.id},
                     reason=a.reason)
    v.status = "INCORPORATED"
    audit.record(db, user.id, "variation.incorporated", "Variation", v.id, project_id=v.project_id,
                 after={"amendment_id": a.id if a else None, "approved_amount": v.approved_amount,
                        "approved_days": v.approved_days})
    return a


# ---------------------------------------------------------------- commercial position (VAR-02)
def best_estimate(v: Variation) -> tuple[Decimal | None, str]:
    for field in ("recommended_amount", "assessed_amount", "claimed_amount"):
        val = getattr(v, field)
        if val is not None:
            return val, field.replace("_amount", "")
    return None, "none"


def position(db: Session, sub: Subcontract) -> dict:
    vs = list(db.scalars(select(Variation).where(Variation.subcontract_id == sub.id)))
    zero = Decimal("0.00")
    original = Decimal((sub.baseline or {}).get("contract_value") or sub.contract_value)
    incorporated = sum((v.approved_amount or zero for v in vs if v.status in ("INCORPORATED",)) , zero)
    incorporated += sum((v.approved_amount or zero for v in vs if v.status == "CLOSED" and v.amendment_id), zero)
    approved_pending = sum((v.approved_amount or zero for v in vs if v.status == "APPROVED"), zero)
    exposure, omissions = zero, zero
    for v in vs:
        if v.status in OPEN_STATES:
            est, _ = best_estimate(v)
            if est is not None and est < 0:
                omissions += est
            else:
                exposure += est or zero
    current = Decimal(sub.contract_value)
    days_granted = sum(v.approved_days or 0 for v in vs if v.status in ("APPROVED", "INCORPORATED")
                       or (v.status == "CLOSED" and v.amendment_id))
    days_open = sum((v.recommended_days if v.recommended_days is not None else v.assessed_days
                     if v.assessed_days is not None else v.claimed_days) or 0 for v in vs if v.status in OPEN_STATES)
    q = lambda x: str(Decimal(x).quantize(CENT))  # noqa: E731
    return {"currency": sub.currency, "original_value": q(original),
            "other_amendments": q(current - original - incorporated),
            "variations_incorporated": q(incorporated), "current_value": q(current),
            "approved_not_incorporated": q(approved_pending),
            "forecast_final_value": q(current + approved_pending),
            "open_exposure": q(exposure), "open_omissions": q(omissions),
            "time": {"days_granted": days_granted, "days_open": days_open},
            "counts": {s: sum(1 for v in vs if v.status == s) for s in STATES if any(v.status == s for v in vs)},
            "rules": ["Contract value changes only when an approved variation is incorporated (by amendment).",
                      "Forecast = current value + approved variations not yet incorporated.",
                      "Open variations are shown at their latest estimate (recommended, else assessed, else "
                      "claimed) and are not added to the forecast; additions and omissions are reported separately, "
                      "never netted.",
                      "The executed baseline is never edited."]}
