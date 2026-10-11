"""Load the demo pilot through the public API, so every invariant and audit rule applies.

    python -m seed.demo_seed            # fresh demo database in ./var
All organisations, people and values are fictional.
"""
import shutil
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

SAMPLES = Path(__file__).parent / "sample_documents"

USERS = [  # username, full name, global roles, clearance
    ("sys.admin", "System Administrator", ["system_admin"], "INTERNAL"),
    ("priya.coord", "Priya Nair - Project Coordinator", ["project_coordinator"], "RESTRICTED"),
    ("ravi.registry", "Ravi Kumar - Registry Manager", ["registry_manager"], "CONFIDENTIAL"),
    ("anita.contracts", "Anita Desai - Contracts Manager", [], "RESTRICTED"),
    ("vikram.legal", "Vikram Rao - Legal Reviewer", [], "RESTRICTED"),
    ("meera.approver", "Meera Iyer - Contracts Approver", [], "RESTRICTED"),
    ("arjun.qaqc", "Arjun Mehta - QA/QC Manager", [], "CONFIDENTIAL"),
    ("sana.hse", "Sana Khan - HSE Manager", [], "CONFIDENTIAL"),
    ("dev.audit", "Dev Patel - Auditor", [], "RESTRICTED"),
    ("omar.solar", "Omar Sheikh - Solar project team", [], "CONFIDENTIAL"),
]
P101_ROLES = {"anita.contracts": ["contracts_manager"], "vikram.legal": ["legal_reviewer"],
              "meera.approver": ["contracts_approver"], "arjun.qaqc": ["qaqc_manager"],
              "sana.hse": ["hse_manager"], "dev.audit": ["auditor"], "ravi.registry": ["registry_manager"]}


def create_users(db, temporary_password: str | None = None):
    """Create the demo users. With a temporary password, every user must change it at first sign-in."""
    from app import auth
    from app.models import User
    hashed = auth.hash_password(temporary_password) if temporary_password else None
    for username, name, roles, clr in USERS:
        u = db.scalar(select(User).where(User.username == username))
        if not u:
            u = User(username=username, full_name=name, roles=roles, clearance=clr)
            db.add(u)
        u.password_hash, u.must_change_password = hashed, bool(hashed)
    db.commit()


def ok(r, *codes):
    if r.status_code not in (codes or (200, 201)):
        raise RuntimeError(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text}")
    return r.json()


def upload(c, who, project_id, path: Path, number, rev, dtype, **extra):
    with open(path, "rb") as f:
        return ok(c.post("/api/v1/documents", headers={"X-User": who},
                         data={"project_id": project_id, "document_number": number, "revision": rev,
                               "document_type": dtype, "title": extra.pop("title", path.stem), **extra},
                         files={"file": (path.name, f.read())}))


def run(c):
    H = lambda u: {"X-User": u}   # noqa: E731
    # Registry ---------------------------------------------------------------
    shakti = ok(c.post("/api/v1/subcontractors", headers=H("ravi.registry"), json={
        "legal_name": "Shakti Piping Works Pvt Ltd", "registration_number": "U28100GJ2011PTC066123",
        "country_code": "IN", "disciplines": ["PIPING", "MECHANICAL"]}))
    deccan = ok(c.post("/api/v1/subcontractors", headers=H("ravi.registry"), json={
        "legal_name": "Deccan Civil Constructions Ltd", "registration_number": "U45200TG2009PLC064871",
        "country_code": "IN", "disciplines": ["CIVIL"]}))
    ok(c.post("/api/v1/subcontractors", headers=H("ravi.registry"), json={
        "legal_name": "Konkan Electricals LLP", "registration_number": "AAK-4471", "country_code": "IN",
        "disciplines": ["ELECTRICAL"]}))

    # Projects, memberships, main contracts ---------------------------------
    p101 = ok(c.post("/api/v1/projects", headers=H("priya.coord"), json={
        "project_code": "P-101", "name": "Green Hydrogen & Ammonia Plant - Phase 1 (demo)",
        "sector_code": "PROCESS", "location": "Kakinada, Andhra Pradesh"}))
    p202 = ok(c.post("/api/v1/projects", headers=H("priya.coord"), json={
        "project_code": "P-202", "name": "Solar BOP Package (demo)", "sector_code": "POWER",
        "location": "Bhadla, Rajasthan"}))
    for u, roles in P101_ROLES.items():
        ok(c.post(f"/api/v1/projects/{p101['id']}/members", headers=H("priya.coord"), json={"username": u, "roles": roles}))
    ok(c.post(f"/api/v1/projects/{p202['id']}/members", headers=H("priya.coord"),
              json={"username": "omar.solar", "roles": ["contracts_manager"]}))
    mc101 = ok(c.post(f"/api/v1/projects/{p101['id']}/main-contracts", headers=H("priya.coord"), json={
        "contract_reference": "EPC-101", "employer_name": "Demo Green Energy Ltd", "effective_date": "2026-06-01"}))
    ok(c.post(f"/api/v1/projects/{p202['id']}/main-contracts", headers=H("priya.coord"), json={
        "contract_reference": "EPC-202", "employer_name": "Demo Solar Parks Ltd", "effective_date": "2026-07-15"}))
    # One enterprise identity, two project relationships (REG-02)
    ok(c.post(f"/api/v1/projects/{p101['id']}/subcontractors", headers=H("priya.coord"),
              json={"subcontractor_id": shakti["id"], "package": "Unit 100 process piping"}))
    ok(c.post(f"/api/v1/projects/{p101['id']}/subcontractors", headers=H("priya.coord"),
              json={"subcontractor_id": deccan["id"], "package": "Pipe-rack foundations"}))
    ok(c.post(f"/api/v1/projects/{p202['id']}/subcontractors", headers=H("priya.coord"),
              json={"subcontractor_id": shakti["id"], "package": "Inverter-station mechanical works"}))

    # Main-contract documents -> governing revisions -------------------------
    pid, mcid = p101["id"], mc101["id"]
    gcc = upload(c, "anita.contracts", pid, SAMPLES / "EPC-101-GCC_revA.docx", "EPC-101-GCC", "A", "GCC",
                 title="General Conditions of Contract", main_contract_id=mcid)
    pc = upload(c, "anita.contracts", pid, SAMPLES / "EPC-101-PC_rev0.txt", "EPC-101-PC", "0", "SPECIAL_CONDITIONS",
                title="Particular Conditions of Contract", main_contract_id=mcid)
    for d in (gcc, pc):
        ok(c.post(f"/api/v1/revisions/{d['revision_id']}/approve", headers=H("vikram.legal"), json={}))

    # Subcontract SC-101-01 (piping): full vertical slice --------------------
    sc1 = ok(c.post("/api/v1/subcontracts", headers={**H("anita.contracts"), "Idempotency-Key": "seed-sc-101-01"}, json={
        "project_id": pid, "main_contract_id": mcid, "subcontractor_id": shakti["id"],
        "subcontract_number": "SC-101-01", "scope_summary": "Fabrication, erection and testing of Unit 100 process piping",
        "discipline": "PIPING", "currency": "INR", "contract_value": "125000000.00"}))
    boq = upload(c, "anita.contracts", pid, SAMPLES / "SC-101-01-BOQ_rev0.xlsx", "SC-101-01-BOQ", "0", "BOQ",
                 title="Bill of Quantities - Piping", subcontract_id=sc1["id"], source_party="EPC_CONTRACTOR")
    ok(c.post(f"/api/v1/revisions/{boq['revision_id']}/approve", headers=H("vikram.legal"), json={}))
    rows = ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/flowdown-mappings/generate", headers=H("anita.contracts")))
    overrides = {
        "8.7": ("COMMERCIAL_DEVIATION", "LDs flowed down at 0.5%/week capped at 7.5% of subcontract value (approved deviation).",
                "Subcontract LDs 0.5% per week, cap 7.5% of Subcontract Price, mapped to piping mechanical-completion milestone."),
        "15.2": ("ADAPTED", "Termination for default flows down with subcontract cure period of 7 days.",
                 "Contractor may terminate after 7-day cure notice for abandonment or persistent default."),
        "20.1": ("ADAPTED", "Arbitration flows down; joinder with main-contract disputes permitted.",
                 "Arbitration under the 1996 Act, seat New Delhi, with joinder provision."),
        "17.1": ("PROJECT_SPECIFIC", "Indemnity limited to subcontract scope; liability cap 100% of subcontract value.",
                 "Special Condition SC-7: indemnity and cap."),
        "18.1": ("PROJECT_SPECIFIC", "CAR and third-party insurance sized to subcontract value.",
                 "Special Condition SC-8: insurances and certificates within 14 days of Commencement Date."),
    }
    for m in rows:
        disp, why, treat = overrides.get(m["clause_reference"], (m["proposed_disposition"],
                                                                 "Proposal reviewed and accepted.", None))
        if m["source_document"] == "EPC-101-GCC" and m["clause_reference"] == "8.4":
            disp, why, treat = "NOT_APPLICABLE", ("Amended by EPC-101-PC cl. 8.4, which takes precedence; flowed "
                                                  "down under that row."), None
        if m["source_document"] == "EPC-101-PC" and m["clause_reference"] == "8.4":
            disp, why, treat = "ADAPTED", "PC amends GCC 8.4 to 10 days; subcontract notice set at 7 days.", \
                               "Subcontractor shall notify the Contractor within 7 days of becoming aware of a delay event."
        ok(c.post(f"/api/v1/flowdown-mappings/{m['id']}/decision", headers=H("vikram.legal"),
                  json={"disposition": disp, "rationale": why, "subcontract_treatment": treat}))
    for target, who in (("TECHNICAL_REVIEW", "anita.contracts"), ("COMMERCIAL_REVIEW", "anita.contracts"),
                        ("LEGAL_REVIEW", "anita.contracts"), ("APPROVAL_PENDING", "vikram.legal"),
                        ("APPROVED_FOR_EXECUTION", "meera.approver")):
        ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/transitions", headers=H(who), json={"target": target}))
    signed = upload(c, "anita.contracts", pid, SAMPLES / "SC-101-01-Agreement_signed.docx", "SC-101-01-AGR", "0",
                    "SUBCONTRACT", title="Subcontract Agreement (signed)", subcontract_id=sc1["id"],
                    source_party="EPC_CONTRACTOR")
    ok(c.post(f"/api/v1/revisions/{signed['revision_id']}/approve", headers=H("vikram.legal"), json={}))
    for target in ("EXECUTED", "ACTIVE"):
        ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/transitions", headers=H("anita.contracts"), json={"target": target}))

    obligations = ok(c.get(f"/api/v1/subcontracts/{sc1['id']}/obligations", headers=H("anita.contracts")))
    by_type = {}
    for o in obligations:
        by_type.setdefault(o["type"], o)
    # A delay event 20 days ago starts the notice clock -> overdue on the dashboard
    if "NOTICE" in by_type:
        ok(c.post(f"/api/v1/obligations/{by_type['NOTICE']['id']}/trigger", headers=H("anita.contracts"),
                  json={"occurred_on": (date.today() - timedelta(days=20)).isoformat(),
                        "note": "Work front PR-3 not released (see LTR-SC-0042)"}))
    # Evidence closure: hydrotest report accepted; HSE plan awaiting review
    ht = upload(c, "arjun.qaqc", pid, SAMPLES / "QA-HT-0007_Hydrotest_Report.pdf", "QA-HT-0007", "0", "TEST_REPORT",
                title="Hydrostatic test report TP-0007", subcontract_id=sc1["id"], source_party="SUBCONTRACTOR")
    if "TESTING" in by_type:
        ev = ok(c.post(f"/api/v1/obligations/{by_type['TESTING']['id']}/evidence", headers=H("arjun.qaqc"),
                       json={"document_revision_id": ht["revision_id"], "note": "Test pack TP-0007"}))
        ok(c.post(f"/api/v1/evidence/{ev['id']}/review", headers=H("anita.contracts"),
                  json={"decision": "ACCEPT", "comment": "Test pressure 1.5x design, calibrated gauges, witnessed."}))
    hse = upload(c, "sana.hse", pid, SAMPLES / "HSE-PLAN-SC-101-01_rev0.txt", "HSE-PLAN-SC-101-01", "0", "HSE_PLAN",
                 title="Site-specific HSE plan", subcontract_id=sc1["id"], source_party="SUBCONTRACTOR")
    if "HSE" in by_type:
        ok(c.post(f"/api/v1/obligations/{by_type['HSE']['id']}/evidence", headers=H("sana.hse"),
                  json={"document_revision_id": hse["revision_id"], "note": "HSE plan rev 0 submitted"}))
    upload(c, "anita.contracts", pid, SAMPLES / "LTR-SC-0042_Correspondence.txt", "LTR-SC-0042", "0", "CORRESPONDENCE",
           title="Letter: delayed work front PR-3", subcontract_id=sc1["id"], source_party="SUBCONTRACTOR")
    ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/obligations", headers=H("anita.contracts"), json={
        "statement": "Internal review of monthly payment application within 5 days of receipt",
        "internal_control_origin": "Commercial procedure CP-04 (internal target, not a contractual deadline)"}))
    am = ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/amendments", headers=H("anita.contracts"), json={
        "field": "contract_value", "new_value": "126850000.00", "reason": "Amendment 1: added supports scope (VO-001)"}))
    ok(c.post(f"/api/v1/amendments/{am['id']}/approve", headers=H("meera.approver")))

    # Subcontract SC-101-02 (civil): stuck at the release gate --------------
    sc2 = ok(c.post("/api/v1/subcontracts", headers=H("anita.contracts"), json={
        "project_id": pid, "main_contract_id": mcid, "subcontractor_id": deccan["id"],
        "subcontract_number": "SC-101-02", "scope_summary": "Pipe-rack foundations and civil works",
        "discipline": "CIVIL", "contract_value": "48000000.00"}))
    rows2 = ok(c.post(f"/api/v1/subcontracts/{sc2['id']}/flowdown-mappings/generate", headers=H("anita.contracts")))
    for m in rows2:
        if m["risk_level"] != "HIGH" and m["proposed_disposition"] != "UNRESOLVED":
            ok(c.post(f"/api/v1/flowdown-mappings/{m['id']}/decision", headers=H("vikram.legal"),
                      json={"disposition": m["proposed_disposition"], "rationale": "Proposal accepted."}))
    for target in ("TECHNICAL_REVIEW", "COMMERCIAL_REVIEW", "LEGAL_REVIEW", "APPROVAL_PENDING"):
        ok(c.post(f"/api/v1/subcontracts/{sc2['id']}/transitions", headers=H("anita.contracts"), json={"target": target}))
    return {"project_id": pid, "main_contract_id": mcid, "sc1": sc1["id"], "sc2": sc2["id"], "p202": p202["id"],
            "shakti": shakti["id"]}


def main():
    from fastapi.testclient import TestClient

    from app import auth, config, db as dbmod
    from app.main import create_app
    if config.is_sqlite() and config.DATA_DIR.exists():
        shutil.rmtree(config.DATA_DIR)
    app = create_app(config.DATABASE_URL)
    dbmod.reset_database()
    shutil.rmtree(config.OBJECT_STORE_DIR, ignore_errors=True)
    config.OBJECT_STORE_DIR.mkdir(parents=True, exist_ok=True)
    with dbmod.SessionLocal() as db:
        create_users(db)                       # passwords are set after loading (see below)
    config.ALLOW_DEV_AUTH = True               # in-process only: the loader drives the API as each user
    ids = run(TestClient(app))
    config.ALLOW_DEV_AUTH = False
    temp = auth.temporary_password()
    with dbmod.SessionLocal() as db:
        create_users(db, temp)
    creds = config.DATA_DIR / "INITIAL_CREDENTIALS.txt"
    creds.write_text("Subcontract OS demo - temporary credentials (change at first sign-in)\n"
                     f"Password for every demo user: {temp}\n\nUsers:\n" +
                     "".join(f"  {u[0]:<18} {u[1]}\n" for u in USERS), encoding="utf-8")
    db_kind = "SQLite" if config.is_sqlite() else "PostgreSQL"
    print(f"Demo loaded into {db_kind}: {ids}")
    print(f"\nTemporary password for ALL demo users: {temp}")
    print(f"Saved to {creds}. Each user must change it at first sign-in.")


if __name__ == "__main__":
    main()
