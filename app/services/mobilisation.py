"""Mobilisation checklist, start-work gate and site-enabler lateness (blueprint 5.2 and 6.1, ADR-0009).

- Executing a subcontract generates its mobilisation checklist from a standard template plus a discipline
  pack (configuration, not code paths per sector). Items can be added by hand.
- An item closes only when a second person verifies submitted evidence, or when a contracts approver records
  an exception with a reason (WF-02). A site item tied to an enabler closes only once that enabler is READY.
- EXECUTED -> ACTIVE (start of work) is blocked while any mandatory item is open.
- An enabler's lateness is measured against each subcontract's need-by date. Lateness is a fact; whether it
  gives entitlement is decided on a variation, never here.
"""
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Enabler, EnablerDependency, EnablerLink, MobilisationItem, Subcontract

COMMERCIAL = ["commercial_manager", "contracts_manager"]
SITE = ["construction_manager", "project_coordinator"]
QA, HSE = ["qaqc_manager"], ["hse_manager"]

# code, category, title, responsible, mandatory, due offset (days after execution), evidence required, verifiers, enabler type
STANDARD = [
    ("DOC-01", "SECURITY", "Performance security (bank guarantee) received and confirmed", "SUBCONTRACTOR", True, 14,
     "Guarantee copy and issuing bank's confirmation", COMMERCIAL, None),
    ("DOC-02", "INSURANCE", "Insurances in force (contractor's all risk, third party, employee compensation)",
     "SUBCONTRACTOR", True, 14, "Policy schedules or certificates naming the project", COMMERCIAL, None),
    ("DOC-03", "HSE", "Site HSE plan approved", "SUBCONTRACTOR", True, 10, "Approved HSE plan revision", HSE, None),
    ("DOC-04", "QUALITY", "Quality plan and inspection & test plans approved", "SUBCONTRACTOR", True, 14,
     "Approved quality plan / ITPs", QA, None),
    ("DOC-05", "PERSONNEL", "Key personnel approved (site manager, HSE officer, QC inspector)", "SUBCONTRACTOR", True,
     10, "Approved CVs or approval letter", SITE, None),
    ("RES-01", "PROGRAMME", "Mobilisation programme and manpower histogram submitted", "SUBCONTRACTOR", True, 7,
     "Programme and histogram", ["project_controls", "construction_manager", "project_coordinator"], None),
    ("RES-02", "STATUTORY", "Statutory labour registrations in place for the site workforce", "SUBCONTRACTOR", True,
     14, "Registration certificates (e.g. labour licence, social-security registrations)", COMMERCIAL, None),
    ("RES-03", "EQUIPMENT", "Equipment and lifting gear inspected; third-party certificates valid", "SUBCONTRACTOR",
     True, 14, "Inspection register and certificates", HSE + SITE, None),
    ("SITE-01", "ACCESS", "Workforce inducted; gate passes issued", "SUBCONTRACTOR", True, 14,
     "Induction register", HSE + SITE, None),
    ("SITE-02", "WORK_FRONT", "Initial work front handed over to the subcontractor", "EPC", True, 14,
     "Handover record (or the work-front enabler verified ready)", SITE, "WORK_FRONT"),
    ("SITE-03", "UTILITIES", "Construction power and water available at the work area", "EPC", True, 14,
     "Enabler verified ready", SITE, "POWER"),
    ("SITE-04", "LAYDOWN", "Laydown and storage area allocated", "EPC", False, 14, "Allocation record", SITE,
     "LAYDOWN_AREA"),
    ("SITE-05", "WELFARE", "Site office and welfare facilities set up", "SUBCONTRACTOR", False, 21,
     "Inspection record", HSE + SITE, None),
]
DISCIPLINE_PACKS = {
    "PIPING": [("PIP-01", "QUALITY", "Welding procedures and welder qualifications approved", "SUBCONTRACTOR", True,
                14, "Approved WPS/PQR and welder qualification records", QA, None),
               ("PIP-02", "QUALITY", "NDT agency approved; calibration certificates valid", "SUBCONTRACTOR", False,
                21, "Approval letter and certificates", QA, None)],
    "MECHANICAL": [("MEC-01", "QUALITY", "Alignment and torque tools calibrated", "SUBCONTRACTOR", False, 21,
                    "Calibration certificates", QA, None)],
    "CIVIL": [("CIV-01", "QUALITY", "Concrete mix designs and material sources approved", "SUBCONTRACTOR", True, 14,
               "Approved mix designs", QA, None)],
    "ELECTRICAL": [("ELE-01", "HSE", "Electrical permit-to-work authorisations for competent persons", "SUBCONTRACTOR",
                    True, 10, "Authorisation register", HSE, None),
                   ("ELE-02", "QUALITY", "Test instruments calibrated (insulation, earth, loop testers)",
                    "SUBCONTRACTOR", True, 14, "Calibration certificates", QA, None)],
}
ENABLER_TYPES = ["WORK_FRONT", "ACCESS_ROAD", "POWER", "WATER", "COMPRESSED_AIR", "LAYDOWN_AREA", "STORES", "OFFICE",
                 "LABOUR_CAMP", "CRANE", "SCAFFOLDING", "PERMIT_SYSTEM", "OTHER"]
CATEGORIES = sorted({t[1] for t in STANDARD} | {t[1] for p in DISCIPLINE_PACKS.values() for t in p} | {"OTHER"})
CLOSED = ("VERIFIED", "WAIVED")


def err(status, code, message, **extra):
    raise HTTPException(status, {"code": code, "message": message, **extra})


def iso(d: str | None, field: str) -> str | None:
    if not d:
        return None
    try:
        return date.fromisoformat(d).isoformat()
    except ValueError:
        err(422, "INVALID_DATE", f"{field} must be YYYY-MM-DD")


# ---------------------------------------------------------------- checklist
def template_for(discipline: str | None) -> list[tuple]:
    return STANDARD + DISCIPLINE_PACKS.get((discipline or "").upper(), [])


def generate(db: Session, sub: Subcontract, actor_id: str) -> int:
    """Idempotent: items already present (by code) are left alone."""
    have = {i.item_code for i in db.scalars(select(MobilisationItem).where(MobilisationItem.subcontract_id == sub.id))}
    start = (sub.executed_at.date() if sub.executed_at else date.today())
    n, added = len(have), 0
    for code, cat, title, resp, mand, offset, ev, verifiers, _etype in template_for(sub.discipline):
        if code in have:
            continue
        n += 1
        added += 1
        db.add(MobilisationItem(subcontract_id=sub.id, item_code=code, line_no=n, category=cat, title=title,
                                responsible_party=resp, mandatory=mand, evidence_required=ev, verifier_roles=verifiers,
                                due_date=(start + timedelta(days=offset)).isoformat()))
    if added:
        audit.record(db, actor_id, "mobilisation.checklist_generated", "Subcontract", sub.id,
                     project_id=sub.project_id, after={"items": added, "discipline": sub.discipline})
    return added


def readiness(db: Session, sub: Subcontract, today: date | None = None) -> dict:
    today = (today or date.today()).isoformat()
    items = list(db.scalars(select(MobilisationItem).where(MobilisationItem.subcontract_id == sub.id)
                            .order_by(MobilisationItem.line_no)))
    mand = [i for i in items if i.mandatory]
    blocking = [i for i in mand if i.status not in CLOSED]
    return {"items": len(items), "mandatory": len(mand), "mandatory_closed": len(mand) - len(blocking),
            "closed": sum(i.status in CLOSED for i in items), "waived": sum(i.status == "WAIVED" for i in items),
            "awaiting_verification": sum(i.status == "SUBMITTED" for i in items),
            "overdue": sum(i.status not in CLOSED and i.due_date and i.due_date < today for i in items),
            "percent": round(100 * (len(mand) - len(blocking)) / len(mand)) if mand else 100,
            "gate_passed": bool(items) and not blocking,
            "blocking": [{"id": i.id, "code": i.item_code, "title": i.title, "status": i.status} for i in blocking]}


def start_work_gate(db: Session, sub: Subcontract) -> None:
    """EXECUTED -> ACTIVE: every mandatory mobilisation item verified or waived by an approver."""
    r = readiness(db, sub)
    if not r["items"]:
        generate(db, sub, sub.created_by)
        db.flush()
        r = readiness(db, sub)
    if not r["gate_passed"]:
        err(409, "MOBILISATION_INCOMPLETE", f"{len(r['blocking'])} mandatory mobilisation item(s) still open: "
            + ", ".join(b["code"] for b in r["blocking"]), readiness=r)


# ---------------------------------------------------------------- enablers
def effective_date(e: Enabler, today: date | None = None) -> str:
    """When the enabler is (or is expected to be) available. A not-ready enabler is never earlier than today."""
    today = (today or date.today()).isoformat()
    if e.status in ("READY", "READY_CLAIMED") and e.actual_date:
        return e.actual_date
    return max(e.forecast_date or e.planned_date, today)


def link_view(e: Enabler, link: EnablerLink, today: date | None = None) -> dict:
    eff = effective_date(e, today)
    late = (date.fromisoformat(eff) - date.fromisoformat(link.need_by_date)).days
    ready = e.status == "READY"
    state = ("ON_TIME" if late <= 0 else "LATE") if ready else ("AT_RISK" if late > 0 else "ON_TRACK")
    return {"id": link.id, "subcontract_id": link.subcontract_id, "need_by": link.need_by_date, "purpose": link.purpose,
            "available": eff, "late_days": max(late, 0), "state": state}


def prerequisites_ready(db: Session, e: Enabler) -> list[Enabler]:
    deps = [db.get(Enabler, d.depends_on_id) for d in
            db.scalars(select(EnablerDependency).where(EnablerDependency.enabler_id == e.id))]
    return [d for d in deps if d.status != "READY"]


def next_enabler_number(db: Session, project_id: str) -> str:
    n = len(list(db.scalars(select(Enabler.id).where(Enabler.project_id == project_id)))) + 1
    return f"EN-{n:03d}"
