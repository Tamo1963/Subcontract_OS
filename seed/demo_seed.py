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
    ("kabir.commercial", "Kabir Shah - Commercial Manager", [], "RESTRICTED"),
    ("rohan.site", "Rohan Iyer - Construction Manager", [], "CONFIDENTIAL"),
    ("arjun.qaqc", "Arjun Mehta - QA/QC Manager", [], "CONFIDENTIAL"),
    ("sana.hse", "Sana Khan - HSE Manager", [], "CONFIDENTIAL"),
    ("dev.audit", "Dev Patel - Auditor", [], "RESTRICTED"),
    ("omar.solar", "Omar Sheikh - Solar project team", [], "CONFIDENTIAL"),
]
P101_ROLES = {"anita.contracts": ["contracts_manager"], "vikram.legal": ["legal_reviewer"],
              "meera.approver": ["contracts_approver"], "kabir.commercial": ["commercial_manager"],
              "rohan.site": ["construction_manager"],
              "arjun.qaqc": ["qaqc_manager"],
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


def decide_rows(c, rows, overrides):
    """Legal decides every flow-down row: overrides first, else the machine proposal is accepted."""
    for m in rows:
        disp, why, treat = overrides.get(m["clause_reference"], (m["proposed_disposition"],
                                                                 "Proposal reviewed and accepted.", None))
        if m["source_document"] == "EPC-101-GCC" and m["clause_reference"] == "8.4":
            disp, why, treat = "NOT_APPLICABLE", ("Amended by EPC-101-PC cl. 8.4, which takes precedence; flowed "
                                                  "down under that row."), None
        if m["source_document"] == "EPC-101-PC" and m["clause_reference"] == "8.4":
            disp, why, treat = "ADAPTED", "PC amends GCC 8.4 to 10 days; subcontract notice set at 7 days.", \
                               "Subcontractor shall notify the Contractor within 7 days of becoming aware of a delay event."
        ok(c.post(f"/api/v1/flowdown-mappings/{m['id']}/decision", headers={"X-User": "vikram.legal"},
                  json={"disposition": disp, "rationale": why, "subcontract_treatment": treat}))


def seed_enablers(c, pid, sc1):
    """Site enablers (ADR-0009). Dates are relative to today so lateness is live."""
    H = lambda u: {"X-User": u}   # noqa: E731
    day = lambda n: (date.today() + timedelta(days=n)).isoformat()  # noqa: E731
    hnd = upload(c, "rohan.site", pid, SAMPLES / "HND-EN-001_Work_Front_Handover.txt", "HND-EN-001", "0",
                 "CORRESPONDENCE", title="Work-front handover certificate PR-1/PR-2", source_party="EPC_CONTRACTOR")
    ok(c.post(f"/api/v1/revisions/{hnd['revision_id']}/approve", headers=H("vikram.legal"), json={}))
    spec = [  # number order matters: EN-001..EN-007
        ("WORK_FRONT", "Work fronts PR-1 and PR-2 (pipe rack)", "Unit 100 pipe rack", "", -60, -58, [(sc1, -55, "piping erection start")]),
        ("POWER", "Construction power 415 V, Unit 100 (DB-07)", "Unit 100", "250 kVA", -60, -57, [(sc1, -55, "welding sets and tools")]),
        ("LAYDOWN_AREA", "Laydown and pipe-spool yard LD-2", "North-east yard", "6,000 m2", -65, -62, [(sc1, -55, "spool storage")]),
        ("WORK_FRONT", "Work front PR-3 (pipe rack)", "Unit 100 pipe rack, grid 12-20", "", -45, -14, [(sc1, -45, "PR-3 piping erection")]),
        ("WATER", "Hydrotest water supply with filling pumps", "Unit 100 test point TW-3", "120 m3/h", 20, None, [(sc1, 25, "hydrotest of test packs TP-0010 onwards")]),
        ("WORK_FRONT", "Substation SS-1 building handed over for E&I", "Substation SS-1", "", -5, None, []),
        ("CRANE", "250 t crawler crane for PR-4 heavy lifts", "Unit 100", "250 t", 40, None, [(sc1, 45, "PR-4 module lifts")]),
    ]
    ids = {}
    for n, (etype, name, loc, cap, planned, ready, links) in enumerate(spec, 1):
        e = ok(c.post(f"/api/v1/projects/{pid}/enablers", headers=H("rohan.site"), json={
            "enabler_type": etype, "name": name, "location": loc, "capacity": cap, "planned_date": day(planned),
            "responsible_party": "EPC", "cost_allocation": "EPC",
            "depends_on": [ids["EN-002"]] if n == 5 else []}))
        ids[e["number"]] = e["id"]
        for sid, need, why in links:
            ok(c.post(f"/api/v1/enablers/{e['id']}/links", headers=H("rohan.site"),
                      json={"subcontract_id": sid, "need_by_date": day(need), "purpose": why}))
        if ready is not None:
            ok(c.post(f"/api/v1/enablers/{e['id']}/ready", headers=H("rohan.site"),
                      json={"actual_date": day(ready), "document_revision_id": hnd["revision_id"]}))
            ok(c.post(f"/api/v1/enablers/{e['id']}/verify", headers=H("priya.coord"), json={"decision": "ACCEPT"}))
    ok(c.patch(f"/api/v1/enablers/{ids['EN-005']}", headers=H("rohan.site"), json={"forecast_date": day(35)}))
    ok(c.patch(f"/api/v1/enablers/{ids['EN-006']}", headers=H("rohan.site"), json={"forecast_date": day(12),
              "notes": "Cable trench civil works late; building roofing incomplete."}))
    return ids


def mobilise(c, pid, sid, dossier_no, enablers, complete):
    """Submit the dossier as evidence and have the right second person verify each item."""
    H = lambda u: {"X-User": u}   # noqa: E731
    dossier = upload(c, "anita.contracts", pid, SAMPLES / f"{dossier_no}_Mobilisation_Dossier.txt", dossier_no, "0",
                     "CORRESPONDENCE", title="Mobilisation dossier", subcontract_id=sid, source_party="SUBCONTRACTOR")
    items = ok(c.get(f"/api/v1/subcontracts/{sid}/mobilisation", headers=H("anita.contracts")))["items"]
    verifier = {"commercial_manager": "kabir.commercial", "contracts_manager": "kabir.commercial",
                "hse_manager": "sana.hse", "qaqc_manager": "arjun.qaqc", "construction_manager": "rohan.site",
                "project_coordinator": "rohan.site", "project_controls": "rohan.site"}
    site_enabler = {"SITE-02": "EN-001", "SITE-03": "EN-002", "SITE-04": "EN-003"} if complete else \
                   {"SITE-02": "EN-006", "SITE-03": "EN-002"}
    plan = ({} if complete else {"DOC-01": "verify", "DOC-02": "verify", "DOC-03": "verify", "DOC-05": "verify",
                                 "RES-01": "verify", "ELE-02": "submit", "DOC-04": "submit", "RES-02": "submit"})
    for it in items:
        code = it["code"]
        if code in site_enabler:
            ok(c.post(f"/api/v1/mobilisation-items/{it['id']}/enabler", headers=H("anita.contracts"),
                      json={"enabler_id": enablers[site_enabler[code]]}))
        action = "verify" if complete else plan.get(code)
        if code == "SITE-05" and complete:
            ok(c.post(f"/api/v1/mobilisation-items/{it['id']}/exception", headers=H("meera.approver"), json={
                "reason": "Shared EPC site office used until the subcontractor's cabins arrive; welfare checked by HSE."}))
            continue
        if not action:
            continue
        if code in site_enabler:
            r = c.post(f"/api/v1/mobilisation-items/{it['id']}/submit", headers=H("rohan.site"), json={})
            if r.status_code != 200:
                continue          # enabler not ready yet: the item stays open
        else:
            ok(c.post(f"/api/v1/mobilisation-items/{it['id']}/submit", headers=H("anita.contracts"),
                      json={"document_revision_id": dossier["revision_id"], "note": f"Dossier section for {code}"}))
        if action == "verify":
            who = verifier[it["verifier_roles"][0]]
            if code in site_enabler:
                who = "priya.coord"
            ok(c.post(f"/api/v1/mobilisation-items/{it['id']}/verify", headers=H(who), json={"decision": "ACCEPT"}))


def seed_sc3(c, pid, mcid, konkan_id, enablers):
    """SC-101-03 (E&I): executed, still mobilising; held by the late substation handover (EN-006)."""
    H = lambda u: {"X-User": u}   # noqa: E731
    sc3 = ok(c.post("/api/v1/subcontracts", headers={**H("anita.contracts"), "Idempotency-Key": "seed-sc-101-03"}, json={
        "project_id": pid, "main_contract_id": mcid, "subcontractor_id": konkan_id, "subcontract_number": "SC-101-03",
        "scope_summary": "Electrical and instrumentation installation, Unit 100", "discipline": "ELECTRICAL",
        "currency": "INR", "contract_value": "48000000.00"}))
    rows = ok(c.post(f"/api/v1/subcontracts/{sc3['id']}/flowdown-mappings/generate", headers=H("anita.contracts")))
    decide_rows(c, rows, {
        "8.7": ("COMMERCIAL_DEVIATION", "LDs flowed down at 0.5%/week capped at 5% (approved deviation).",
                "Subcontract LDs 0.5% per week, cap 5% of Subcontract Price, mapped to E&I mechanical completion."),
        "17.1": ("PROJECT_SPECIFIC", "Indemnity limited to subcontract scope; cap 100% of subcontract value.",
                 "Special Condition SC-7: indemnity and cap."),
        "18.1": ("PROJECT_SPECIFIC", "Insurances sized to subcontract value.",
                 "Special Condition SC-8: insurances within 14 days of Commencement Date."),
        "15.2": ("ADAPTED", "Termination for default with 7-day cure period.", "7-day cure notice."),
        "20.1": ("ADAPTED", "Arbitration flows down with joinder.", "Arbitration, seat New Delhi, joinder."),
    })
    for target, who in (("TECHNICAL_REVIEW", "anita.contracts"), ("COMMERCIAL_REVIEW", "anita.contracts"),
                        ("LEGAL_REVIEW", "anita.contracts"), ("APPROVAL_PENDING", "vikram.legal"),
                        ("APPROVED_FOR_EXECUTION", "meera.approver")):
        ok(c.post(f"/api/v1/subcontracts/{sc3['id']}/transitions", headers=H(who), json={"target": target}))
    signed = upload(c, "anita.contracts", pid, SAMPLES / "SC-101-03-Agreement_signed.txt", "SC-101-03-AGR", "0",
                    "SUBCONTRACT", title="Subcontract Agreement (signed)", subcontract_id=sc3["id"],
                    source_party="EPC_CONTRACTOR")
    ok(c.post(f"/api/v1/revisions/{signed['revision_id']}/approve", headers=H("vikram.legal"), json={}))
    ok(c.post(f"/api/v1/subcontracts/{sc3['id']}/transitions", headers=H("anita.contracts"), json={"target": "EXECUTED"}))
    for code, need, why in (("EN-006", 3, "E&I installation start in SS-1"), ("EN-002", 5, "temporary power for SS-1")):
        ok(c.post(f"/api/v1/enablers/{enablers[code]}/links", headers=H("rohan.site"),
                  json={"subcontract_id": sc3["id"], "need_by_date": (date.today() + timedelta(days=need)).isoformat(),
                        "purpose": why}))
    mobilise(c, pid, sc3["id"], "MOB-SC-101-03", enablers, complete=False)
    return sc3


def run(c):
    H = lambda u: {"X-User": u}   # noqa: E731
    # Registry ---------------------------------------------------------------
    shakti = ok(c.post("/api/v1/subcontractors", headers=H("ravi.registry"), json={
        "legal_name": "Shakti Piping Works Pvt Ltd", "registration_number": "U28100GJ2011PTC066123",
        "country_code": "IN", "disciplines": ["PIPING", "MECHANICAL"]}))
    deccan = ok(c.post("/api/v1/subcontractors", headers=H("ravi.registry"), json={
        "legal_name": "Deccan Civil Constructions Ltd", "registration_number": "U45200TG2009PLC064871",
        "country_code": "IN", "disciplines": ["CIVIL"]}))
    konkan = ok(c.post("/api/v1/subcontractors", headers=H("ravi.registry"), json={
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
    ok(c.post(f"/api/v1/projects/{p101['id']}/subcontractors", headers=H("priya.coord"),
              json={"subcontractor_id": konkan["id"], "package": "Unit 100 electrical & instrumentation"}))

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
    decide_rows(c, rows, overrides)
    for target, who in (("TECHNICAL_REVIEW", "anita.contracts"), ("COMMERCIAL_REVIEW", "anita.contracts"),
                        ("LEGAL_REVIEW", "anita.contracts"), ("APPROVAL_PENDING", "vikram.legal"),
                        ("APPROVED_FOR_EXECUTION", "meera.approver")):
        ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/transitions", headers=H(who), json={"target": target}))
    signed = upload(c, "anita.contracts", pid, SAMPLES / "SC-101-01-Agreement_signed.docx", "SC-101-01-AGR", "0",
                    "SUBCONTRACT", title="Subcontract Agreement (signed)", subcontract_id=sc1["id"],
                    source_party="EPC_CONTRACTOR")
    ok(c.post(f"/api/v1/revisions/{signed['revision_id']}/approve", headers=H("vikram.legal"), json={}))
    ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/transitions", headers=H("anita.contracts"), json={"target": "EXECUTED"}))
    enablers = seed_enablers(c, pid, sc1["id"])
    mobilise(c, pid, sc1["id"], "MOB-SC-101-01", enablers, complete=True)
    ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/transitions", headers=H("anita.contracts"), json={"target": "ACTIVE"}))
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
    ltr = upload(c, "anita.contracts", pid, SAMPLES / "LTR-SC-0042_Correspondence.txt", "LTR-SC-0042", "0", "CORRESPONDENCE",
           title="Letter: delayed work front PR-3", subcontract_id=sc1["id"], source_party="SUBCONTRACTOR")
    ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/obligations", headers=H("anita.contracts"), json={
        "statement": "Internal review of monthly payment application within 5 days of receipt",
        "internal_control_origin": "Commercial procedure CP-04 (internal target, not a contractual deadline)"}))
    am = ok(c.post(f"/api/v1/subcontracts/{sc1['id']}/amendments", headers=H("anita.contracts"), json={
        "field": "contract_value", "new_value": "126850000.00", "reason": "Amendment 1: added supports scope (VO-001)"}))
    ok(c.post(f"/api/v1/amendments/{am['id']}/approve", headers=H("meera.approver")))
    seed_variations(c, pid, sc1["id"], ltr["revision_id"], enablers)
    sc3 = seed_sc3(c, pid, mcid, konkan["id"], enablers)

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
    return {"project_id": pid, "main_contract_id": mcid, "sc1": sc1["id"], "sc2": sc2["id"], "sc3": sc3["id"],
            "enablers": enablers, "p202": p202["id"],
            "shakti": shakti["id"]}


def seed_variations(c, pid, sc, ltr_rev, enablers):
    """Three variations at different stages (ADR-0008). Dates are relative to today so the notice clocks live."""
    H = lambda u: {"X-User": u}   # noqa: E731
    day = lambda n: (date.today() - timedelta(days=n)).isoformat()  # noqa: E731
    V = lambda vid, who, path, **kw: ok(c.post(f"/api/v1/variations/{vid}/{path}", headers=H(who), **kw))  # noqa: E731
    to = lambda vid, who, target: V(vid, who, "transitions", json={"target": target})  # noqa: E731

    # VAR-001: design change, notice on time, valued, approved and incorporated (Amendment 2)
    ev1 = ok(c.post(f"/api/v1/projects/{pid}/events", headers=H("arjun.qaqc"), json={
        "event_type": "DESIGN_CHANGE", "occurred_on": day(40), "location": "Pipe rack PR-2", "subcontract_id": sc,
        "description": "IFC revision C of the PR-2 pipe-rack drawings adds 42 type-B pipe supports."}))
    v1 = ok(c.post(f"/api/v1/subcontracts/{sc}/variations", headers={**H("anita.contracts"), "Idempotency-Key": "seed-var-001"},
                   json={"title": "Additional pipe supports on rack PR-2", "origin_event_id": ev1["id"],
                         "category": "DESIGN_CHANGE", "instruction_reference": "IFC rev C, DWG PR2-ST-004",
                         "description": "42 type-B supports added by IFC revision C; not in the tender drawings.",
                         "claimed_amount": "720000.00", "claimed_days": 6}))
    ntc = upload(c, "anita.contracts", pid, SAMPLES / "NTC-SC-0051_Variation_Notice.txt", "NTC-SC-0051", "0",
                 "CORRESPONDENCE", title="Notice of variation: PR-2 supports", subcontract_id=sc,
                 source_party="SUBCONTRACTOR")
    ok(c.post(f"/api/v1/revisions/{ntc['revision_id']}/approve", headers=H("vikram.legal"), json={}))
    to(v1["id"], "anita.contracts", "CLASSIFIED")
    ok(c.patch(f"/api/v1/variations/{v1['id']}", headers=H("anita.contracts"),
               json={"notice_given_on": day(34), "notice_revision_id": ntc["revision_id"]}))
    for t in ("NOTICE_REVIEW", "ENTITLEMENT_ASSESSMENT"):
        to(v1["id"], "anita.contracts", t)
    V(v1["id"], "anita.contracts", "entitlement", json={
        "status": "ENTITLED", "clause_references": ["13.1"],
        "basis": "Change to the issued-for-construction drawings is a variation instructed by the Contractor."})
    to(v1["id"], "anita.contracts", "COST_AND_TIME_ASSESSMENT")
    c.put(f"/api/v1/variations/{v1['id']}/valuation", headers=H("anita.contracts"), json={"assessed_days": 4, "items": [
        {"description": "Fabricate and erect type-B pipe supports", "cost_type": "SUBCONTRACT",
         "pricing_basis": "CONTRACT_RATE", "quantity": "42", "unit": "nos", "rate": "14250.50", "rate_reference": "BOQ 3.12"},
        {"description": "Scaffolding at height for support erection", "cost_type": "PRELIMINARIES",
         "pricing_basis": "QUOTATION", "quantity": "1", "unit": "LS", "rate": "55000.00",
         "rate_reference": "Quotation Q-778"}]}).raise_for_status()
    to(v1["id"], "anita.contracts", "COMMERCIAL_REVIEW")
    V(v1["id"], "kabir.commercial", "recommend", json={
        "amount": "640000.00", "days": 4, "comment": "Scaffolding quotation above market; reduced to the schedule-of-rates level."})
    V(v1["id"], "meera.approver", "decision", json={"decision": "APPROVE", "reason": "As recommended."})
    V(v1["id"], "anita.contracts", "incorporate")

    # VAR-002: standing time at PR-3, notice given LATE -> held at notice review for legal
    ev2 = ok(c.post(f"/api/v1/projects/{pid}/events", headers=H("anita.contracts"), json={
        "event_type": "ACCESS_DELAY", "occurred_on": day(45), "location": "Pipe rack PR-3", "subcontract_id": sc,
        "source_revision_id": ltr_rev, "enabler_id": enablers["EN-004"],
        "description": "Work front PR-3 not released on the planned date; civil foundations incomplete."}))
    v2 = ok(c.post(f"/api/v1/subcontracts/{sc}/variations", headers=H("anita.contracts"), json={
        "title": "Standing time - work front PR-3 not released", "origin_event_id": ev2["id"],
        "initiated_by": "SUBCONTRACTOR", "category": "ACCESS_DELAY",
        "description": "Subcontractor claims crew and equipment standing time while PR-3 was unavailable.",
        "claimed_amount": "1240000.00", "claimed_days": 12}))
    to(v2["id"], "anita.contracts", "CLASSIFIED")
    ok(c.patch(f"/api/v1/variations/{v2['id']}", headers=H("anita.contracts"),
               json={"notice_given_on": day(14), "notice_revision_id": ltr_rev}))
    to(v2["id"], "anita.contracts", "NOTICE_REVIEW")

    # VAR-003: EPC-instructed omission; notice not applicable (legal), priced, awaiting commercial recommendation
    v3 = ok(c.post(f"/api/v1/subcontracts/{sc}/variations", headers=H("anita.contracts"), json={
        "title": "Omit insulation on line 6-P-1104", "category": "SCOPE_OMISSION", "event_date": day(10),
        "instruction_reference": "Site instruction SI-017",
        "description": "Process change: line 6-P-1104 no longer needs hot insulation; scope omitted by SI-017."}))
    to(v3["id"], "anita.contracts", "CLASSIFIED")
    to(v3["id"], "anita.contracts", "NOTICE_REVIEW")
    V(v3["id"], "vikram.legal", "notice-risk", json={
        "decision": "NOTICE_NOT_REQUIRED", "reason": "EPC-instructed omission; no subcontractor claim notice applies."})
    to(v3["id"], "anita.contracts", "ENTITLEMENT_ASSESSMENT")
    V(v3["id"], "anita.contracts", "entitlement", json={
        "status": "ENTITLED", "clause_references": ["13.1"],
        "basis": "The Contractor may omit work by instruction; valued at contract rates."})
    to(v3["id"], "anita.contracts", "COST_AND_TIME_ASSESSMENT")
    c.put(f"/api/v1/variations/{v3['id']}/valuation", headers=H("anita.contracts"), json={"items": [
        {"description": "Hot insulation, 6-inch line, omitted", "cost_type": "SUBCONTRACT", "pricing_basis": "CONTRACT_RATE",
         "quantity": "-180", "unit": "m", "rate": "1850.00", "rate_reference": "BOQ 7.04"}]}).raise_for_status()
    to(v3["id"], "anita.contracts", "COMMERCIAL_REVIEW")


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
