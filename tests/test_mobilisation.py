"""Mobilisation checklist, start-work gate and site enablers (blueprint 5.2, 6.1; acceptance WF-02; ADR-0009)."""
from datetime import date, timedelta

from tests.conftest import H

ANITA, ROHAN, PRIYA, MEERA, KABIR = "anita.contracts", "rohan.site", "priya.coord", "meera.approver", "kabir.commercial"


def day(n):
    return (date.today() + timedelta(days=n)).isoformat()


def call(client, method, who, url, **kw):
    return getattr(client, method)(f"/api/v1{url}", headers=H(who), **kw)


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def checklist(client, sid, who=ANITA):
    return ok(call(client, "get", who, f"/subcontracts/{sid}/mobilisation"))


def item(client, sid, code, who=ANITA):
    return next(i for i in checklist(client, sid, who)["items"] if i["code"] == code)


def a_document(client, world, number="MOB-SC-101-03"):
    docs = ok(call(client, "get", ANITA, f"/projects/{world['project_id']}/documents"))
    return next(d for d in docs if d["document_number"] == number)["revisions"][0]["id"]


def enablers(client, world):
    return {e["number"]: e for e in ok(call(client, "get", ANITA, f"/projects/{world['project_id']}/enablers"))["enablers"]}


def test_checklist_generated_at_execution_with_discipline_pack(client, world):
    sc1, sc3 = checklist(client, world["sc1"]), checklist(client, world["sc3"])
    assert {"PIP-01", "PIP-02"} <= {i["code"] for i in sc1["items"]} and "ELE-01" not in {i["code"] for i in sc1["items"]}
    assert {"ELE-01", "ELE-02"} <= {i["code"] for i in sc3["items"]}
    assert all(i["due_date"] for i in sc3["items"])
    assert ok(call(client, "post", ANITA, f"/subcontracts/{world['sc3']}/mobilisation/generate"))["added"] == 0   # idempotent
    r = call(client, "post", ANITA, f"/subcontracts/{world['sc2']}/mobilisation/generate")
    assert r.json()["error"]["code"] == "SUBCONTRACT_NOT_EXECUTED"


def test_WF02_no_closure_without_evidence_except_by_authorised_exception(client, world):
    i = item(client, world["sc3"], "RES-03")
    r = call(client, "post", ANITA, f"/mobilisation-items/{i['id']}/submit", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "EVIDENCE_REQUIRED"
    body = {"reason": "Lifting gear arrives next week; no lifts planned before then."}
    assert call(client, "post", ANITA, f"/mobilisation-items/{i['id']}/exception", json=body).status_code == 403
    assert call(client, "post", MEERA, f"/mobilisation-items/{i['id']}/exception", json={"reason": "short"}).status_code == 422
    out = ok(call(client, "post", MEERA, f"/mobilisation-items/{i['id']}/exception",
                  json={**body, "expires_on": day(7)}))
    assert out["status"] == "WAIVED" and out["exception"]["by"] == MEERA and out["exception"]["expires_on"] == day(7)
    events = ok(call(client, "get", "dev.audit", f"/audit-events?project_id={world['project_id']}"))
    assert any(e["action"] == "mobilisation.item_waived" and e["actor"] == MEERA for e in events)


def test_verification_is_by_a_second_person_with_the_right_role(client, world):
    i = item(client, world["sc3"], "DOC-04")              # submitted by anita in the demo; QA verifies
    assert i["status"] == "SUBMITTED" and i["verifier_roles"] == ["qaqc_manager"]
    assert call(client, "post", KABIR, f"/mobilisation-items/{i['id']}/verify",
                json={"decision": "ACCEPT"}).status_code == 403                      # wrong discipline
    r = call(client, "post", "arjun.qaqc", f"/mobilisation-items/{i['id']}/verify", json={"decision": "REJECT"})
    assert r.json()["error"]["code"] == "REASON_REQUIRED"
    out = ok(call(client, "post", "arjun.qaqc", f"/mobilisation-items/{i['id']}/verify",
                  json={"decision": "REJECT", "comment": "ITP for cable testing missing"}))
    assert out["status"] == "OPEN" and out["evidence"] is None
    ok(call(client, "post", "arjun.qaqc", f"/mobilisation-items/{i['id']}/submit",
            json={"document_revision_id": a_document(client, world)}))
    r = call(client, "post", "arjun.qaqc", f"/mobilisation-items/{i['id']}/verify", json={"decision": "ACCEPT"})
    assert r.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"                      # submitter cannot verify


def test_start_work_gate_blocks_until_mandatory_items_close(client, world):
    sid = world["sc3"]
    r = call(client, "post", ANITA, f"/subcontracts/{sid}/transitions", json={"target": "ACTIVE"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MOBILISATION_INCOMPLETE"
    blocking = r.json()["error"]["readiness"]["blocking"]
    assert "SITE-02" in [b["code"] for b in blocking]
    for b in blocking:                                       # an approver closes them (recorded exceptions)
        ok(call(client, "post", MEERA, f"/mobilisation-items/{b['id']}/exception",
                json={"reason": "Test: closed by recorded exception for the gate check."}))
    assert ok(call(client, "post", ANITA, f"/subcontracts/{sid}/transitions", json={"target": "ACTIVE"}))["status"] == "ACTIVE"


def test_site_item_waits_for_its_enabler(client, world):
    sid, en = world["sc3"], enablers(client, world)
    i = item(client, sid, "SITE-02")
    assert i["enabler"]["number"] == "EN-006"
    r = call(client, "post", ROHAN, f"/mobilisation-items/{i['id']}/submit", json={})
    assert r.json()["error"]["code"] == "ENABLER_NOT_READY"
    e6 = en["EN-006"]["id"]
    ok(call(client, "post", ROHAN, f"/enablers/{e6}/ready",
            json={"actual_date": day(0), "document_revision_id": a_document(client, world, "HND-EN-001")}))
    assert call(client, "post", ROHAN, f"/enablers/{e6}/verify", json={"decision": "ACCEPT"}).json()["error"]["code"] \
        == "SEGREGATION_OF_DUTIES"
    ok(call(client, "post", PRIYA, f"/enablers/{e6}/verify", json={"decision": "ACCEPT"}))
    out = ok(call(client, "post", ROHAN, f"/mobilisation-items/{i['id']}/submit", json={}))
    assert out["status"] == "SUBMITTED" and out["evidence"]["document_number"] == "HND-EN-001"   # enabler's evidence
    assert ok(call(client, "post", PRIYA, f"/mobilisation-items/{i['id']}/verify",
                   json={"decision": "ACCEPT"}))["status"] == "VERIFIED"


def test_enabler_readiness_rules(client, world):
    pid, en = world["project_id"], enablers(client, world)
    assert call(client, "post", ANITA, f"/projects/{pid}/enablers", json={
        "enabler_type": "WATER", "name": "Potable water", "planned_date": day(5)}).status_code == 403
    dep = ok(call(client, "post", ROHAN, f"/projects/{pid}/enablers", json={
        "enabler_type": "COMPRESSED_AIR", "name": "Instrument air for loop checks", "planned_date": day(2),
        "depends_on": [en["EN-006"]["id"]]}), 201)
    assert dep["number"] == "EN-008" and dep["prerequisites"][0]["number"] == "EN-006"
    url = f"/enablers/{dep['id']}"
    assert call(client, "post", ROHAN, f"{url}/ready", json={"actual_date": day(3),
                "document_revision_id": a_document(client, world, "HND-EN-001")}).json()["error"]["code"] == "FUTURE_DATE"
    assert call(client, "post", ROHAN, f"{url}/ready", json={"actual_date": day(0)}).status_code == 422   # no evidence
    ok(call(client, "post", ROHAN, f"{url}/ready", json={"actual_date": day(0),
            "document_revision_id": a_document(client, world, "HND-EN-001")}))
    r = call(client, "post", PRIYA, f"{url}/verify", json={"decision": "ACCEPT"})
    assert r.json()["error"]["code"] == "PREREQUISITE_NOT_READY"
    out = ok(call(client, "post", PRIYA, f"{url}/verify", json={"decision": "REJECT", "comment": "Compressor not commissioned"}))
    assert out["status"] == "PLANNED" and out["actual_date"] is None


def test_lateness_against_need_by_dates(client, world):
    en = enablers(client, world)
    e4 = en["EN-004"]
    assert e4["flag"] == "LATE" and e4["links"][0]["late_days"] == 31 and e4["links"][0]["subcontract_number"] == "SC-101-01"
    e6 = en["EN-006"]
    assert e6["flag"] == "AT_RISK" and e6["links"][0]["late_days"] == 9
    url = f"/enablers/{e6['id']}"
    assert call(client, "patch", ROHAN, url, json={"planned_date": day(1)}).json()["error"]["code"] == "REASON_REQUIRED"
    out = ok(call(client, "patch", ROHAN, url, json={"forecast_date": day(2)}))
    assert out["flag"] == "ON_TRACK" and out["links"][0]["late_days"] == 0
    assert call(client, "patch", ROHAN, f"/enablers/{en['EN-001']['id']}",
                json={"forecast_date": day(1)}).json()["error"]["code"] == "ALREADY_READY"
    dash = ok(call(client, "get", ANITA, f"/projects/{world['project_id']}/dashboard"))["mobilisation"]
    assert {s["subcontract_number"]: s["gate_passed"] for s in dash["subcontracts"]} == {"SC-101-01": True, "SC-101-03": False}
    assert [f["number"] for f in dash["enabler_flags"]][:2] == ["EN-004", "EN-005"]


def test_delay_event_from_late_enabler_feeds_a_variation(client, world):
    en = enablers(client, world)
    e6 = en["EN-006"]["id"]
    assert call(client, "post", ANITA, f"/enablers/{e6}/delay-event",
                json={"subcontract_id": world["sc1"]}).json()["error"]["code"] == "NOT_LINKED"
    assert call(client, "post", ANITA, f"/enablers/{en['EN-001']['id']}/delay-event",
                json={"subcontract_id": world["sc1"]}).json()["error"]["code"] == "NOT_LATE"
    ev = ok(call(client, "post", ANITA, f"/enablers/{e6}/delay-event", json={"subcontract_id": world["sc3"]}), 201)
    assert ev["type"] == "ACCESS_DELAY" and "9 days late" in ev["description"]
    # VAR-002 in the demo was raised from the late PR-3 work front: its trace reaches the enabler
    vs = {v["number"]: v for v in ok(call(client, "get", ANITA, f"/projects/{world['project_id']}/variations"))["variations"]}
    t = ok(call(client, "get", "dev.audit", f"/variations/{vs['VAR-002']['id']}/trace"))
    assert any(n["kind"] == "Enabler" and n["label"].startswith("EN-004") for n in t["nodes"])
    assert any(e["type"] == "LATENESS_RECORDED_IN" for e in t["edges"])


def test_access_is_project_scoped(client, world):
    pid = world["project_id"]
    assert call(client, "get", "omar.solar", f"/projects/{pid}/enablers").status_code == 403
    assert call(client, "get", "omar.solar", f"/subcontracts/{world['sc3']}/mobilisation").status_code == 403
    listing = ok(call(client, "get", "dev.audit", f"/projects/{pid}/enablers"))
    assert listing["can_manage"] is False and listing["summary"]["total"] == 7
    i = item(client, world["sc3"], "SITE-01", "dev.audit")
    assert i["actions"] == [] and call(client, "post", "dev.audit", f"/mobilisation-items/{i['id']}/submit",
                                       json={"document_revision_id": a_document(client, world)}).status_code == 403
