"""Variations (blueprint 5.3; acceptance VAR-01, VAR-02; ADR-0008)."""
from datetime import date, timedelta

from tests.conftest import H

ANITA, KABIR, MEERA, VIKRAM = "anita.contracts", "kabir.commercial", "meera.approver", "vikram.legal"


def day(n):
    return (date.today() - timedelta(days=n)).isoformat()


def call(client, method, who, url, **kw):
    return getattr(client, method)(f"/api/v1{url}", headers=H(who), **kw)


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def raise_var(client, world, **kw):
    body = {"title": "Extra valves on header 6-P-1201", "category": "SCOPE_ADDITION", "event_date": day(3),
            "description": "Two additional isolation valves instructed by site query SQ-31.",
            "claimed_amount": "300000.00", "claimed_days": 2, **kw}
    return ok(call(client, "post", ANITA, f"/subcontracts/{world['sc1']}/variations", json=body), 201)


def to(client, vid, target, who=ANITA, **kw):
    return call(client, "post", who, f"/variations/{vid}/transitions", json={"target": target, **kw})


def notice_doc(client, world):
    docs = ok(call(client, "get", ANITA, f"/projects/{world['project_id']}/documents"))
    return next(d for d in docs if d["document_number"] == "NTC-SC-0051")["revisions"][0]["id"]


def to_valuation(client, world, **kw):
    v = raise_var(client, world, **kw)
    ok(to(client, v["id"], "CLASSIFIED"))
    ok(call(client, "patch", ANITA, f"/variations/{v['id']}",
            json={"notice_given_on": day(1), "notice_revision_id": notice_doc(client, world)}))
    ok(to(client, v["id"], "NOTICE_REVIEW"))
    ok(to(client, v["id"], "ENTITLEMENT_ASSESSMENT"))
    ok(call(client, "post", ANITA, f"/variations/{v['id']}/entitlement", json={
        "status": "ENTITLED", "basis": "Instructed addition under the variations clause", "clause_references": ["13.1"]}))
    ok(to(client, v["id"], "COST_AND_TIME_ASSESSMENT"))
    return v


def priced(client, world, lines=None, days=2, **kw):
    v = to_valuation(client, world, **kw)
    lines = lines or [{"description": "Supply and fit 6-inch gate valve", "cost_type": "MATERIAL",
                       "pricing_basis": "CONTRACT_RATE", "quantity": "2", "unit": "nos", "rate": "118333.335"}]
    ok(call(client, "put", ANITA, f"/variations/{v['id']}/valuation", json={"items": lines, "assessed_days": days}))
    ok(to(client, v["id"], "COMMERCIAL_REVIEW"))
    return v


def pending(client, world, **kw):
    v = priced(client, world, **kw)
    ok(call(client, "post", KABIR, f"/variations/{v['id']}/recommend", json={}))
    return v


def position(client, world):
    return ok(call(client, "get", ANITA, f"/subcontracts/{world['sc1']}/commercial-position"))


# ---------------------------------------------------------------- VAR-01 notice
def test_VAR01_notice_clock_comes_from_the_subcontracts_own_obligation(client, world):
    v = raise_var(client, world, event_date="2026-01-05")
    obs = ok(call(client, "get", ANITA, f"/subcontracts/{world['sc1']}/obligations"))
    src = [o for o in obs if o["type"] == "VARIATION" and o["notice_period_days"]] or \
          [o for o in obs if o["type"] == "NOTICE" and o["notice_period_days"]]
    n = v["notice"]
    assert n["period_days"] == src[0]["notice_period_days"]
    assert n["due_on"] == (date(2026, 1, 5) + timedelta(days=n["period_days"])).isoformat()
    full = ok(call(client, "get", ANITA, f"/variations/{v['id']}"))
    assert full["notice_obligation"]["number"] == src[0]["number"] and full["notice_obligation"]["source_wording"]


def test_VAR01_missing_notice_is_flagged_and_never_silently_waived(client, world):
    v = raise_var(client, world, event_date=day(60))            # notice period long expired, nothing recorded
    assert v["notice"]["status"] == "MISSING_OVERDUE" and v["notice"]["at_risk"] is True
    ok(to(client, v["id"], "CLASSIFIED"))
    ok(to(client, v["id"], "NOTICE_REVIEW"))
    r = to(client, v["id"], "ENTITLEMENT_ASSESSMENT")
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOTICE_RISK_UNRESOLVED"
    body = {"decision": "PROCEED_DESPITE_NOTICE_RISK", "reason": "Instruction was oral; EPC knew of the event on the day."}
    assert call(client, "post", ANITA, f"/variations/{v['id']}/notice-risk", json=body).status_code == 403
    assert call(client, "post", VIKRAM, f"/variations/{v['id']}/notice-risk",
                json={**body, "reason": "short"}).status_code == 422
    out = ok(call(client, "post", VIKRAM, f"/variations/{v['id']}/notice-risk", json=body))
    assert out["notice"]["status"] == "MISSING_OVERDUE"                       # the fact stays visible
    assert out["notice"]["risk_decision"]["by"] == VIKRAM and out["notice"]["at_risk"] is False
    ok(to(client, v["id"], "ENTITLEMENT_ASSESSMENT"))
    events = ok(call(client, "get", "dev.audit", f"/audit-events?project_id={world['project_id']}"))
    assert any(e["action"] == "variation.notice_risk_decided" and e["actor"] == VIKRAM for e in events)


def test_VAR01_pending_notice_and_late_notice(client, world):
    v = raise_var(client, world, event_date=day(2))
    ok(to(client, v["id"], "CLASSIFIED"))
    ok(to(client, v["id"], "NOTICE_REVIEW"))
    assert to(client, v["id"], "ENTITLEMENT_ASSESSMENT").json()["error"]["code"] == "NOTICE_NOT_YET_GIVEN"
    r = call(client, "post", VIKRAM, f"/variations/{v['id']}/notice-risk",
             json={"decision": "PROCEED_DESPITE_NOTICE_RISK", "reason": "Want to move quickly on this one."})
    assert r.json()["error"]["code"] == "NOTICE_STILL_OPEN"
    late = raise_var(client, world, event_date=day(60))
    ok(call(client, "patch", ANITA, f"/variations/{late['id']}", json={"notice_given_on": day(1)}))
    got = ok(call(client, "get", ANITA, f"/variations/{late['id']}"))["notice"]
    assert got["status"] == "GIVEN_NO_EVIDENCE"                          # a date alone is not proof of notice
    ok(call(client, "patch", ANITA, f"/variations/{late['id']}", json={"notice_revision_id": notice_doc(client, world)}))
    got = ok(call(client, "get", ANITA, f"/variations/{late['id']}"))
    assert got["notice"]["status"] == "GIVEN_LATE" and got["notice"]["at_risk"]
    assert any(e["kind"] == "NOTICE" for e in got["evidence"])


def test_VAR01_demo_variation_held_at_notice_review(client, world):
    vs = ok(call(client, "get", ANITA, f"/projects/{world['project_id']}/variations"))
    by = {v["number"]: v for v in vs["variations"]}
    assert by["VAR-002"]["status"] == "NOTICE_REVIEW" and by["VAR-002"]["notice"]["status"] == "GIVEN_LATE"
    assert by["VAR-003"]["notice"]["status"] == "NOT_REQUIRED" and by["VAR-001"]["status"] == "INCORPORATED"
    dash = ok(call(client, "get", ANITA, f"/projects/{world['project_id']}/dashboard"))["variations"]
    assert dash["notice_at_risk"] == 1 and [a["number"] for a in dash["notice_alerts"]] == ["VAR-002"]


# ---------------------------------------------------------------- VAR-02 forecast
def test_VAR02_only_incorporation_changes_contract_value(client, world):
    before = position(client, world)
    v = pending(client, world)
    mid = position(client, world)
    assert mid["current_value"] == mid["forecast_final_value"] == before["current_value"]   # open = exposure only
    assert float(mid["open_exposure"]) == float(before["open_exposure"]) + 236666.68
    ok(call(client, "post", MEERA, f"/variations/{v['id']}/decision", json={"decision": "APPROVE"}))
    appr = position(client, world)
    assert appr["current_value"] == before["current_value"]                       # value untouched
    assert float(appr["forecast_final_value"]) == float(before["current_value"]) + 236666.68
    assert appr["approved_not_incorporated"] == "236666.68"
    r = ok(call(client, "post", ANITA, f"/variations/{v['id']}/incorporate"))
    assert r["amendment"]["old_value"] == before["current_value"]
    after = r["position"]
    assert float(after["current_value"]) == float(before["current_value"]) + 236666.68
    assert after["approved_not_incorporated"] == "0.00" and after["original_value"] == "125000000.00"
    s = ok(call(client, "get", ANITA, f"/subcontracts/{world['sc1']}"))
    assert s["baseline"]["contract_value"] == "125000000.00"                      # executed baseline never edited
    assert call(client, "post", ANITA, f"/variations/{v['id']}/incorporate").status_code == 409   # not twice


def test_VAR02_omissions_reported_separately_never_netted(client, world):
    p = position(client, world)
    assert p["open_exposure"] == "1240000.00" and p["open_omissions"] == "-333000.00"   # demo VAR-002 and VAR-003
    assert p["forecast_final_value"] == p["current_value"]


# ---------------------------------------------------------------- valuation and approvals
def test_valuation_is_computed_by_the_server(client, world):
    v = to_valuation(client, world)
    url = f"/variations/{v['id']}/valuation"
    line = {"description": "Valve", "cost_type": "MATERIAL", "pricing_basis": "CONTRACT_RATE", "quantity": "3",
            "unit": "nos", "rate": "1000.005", "amount": "999999"}             # a sent amount is ignored
    out = ok(call(client, "put", ANITA, url, json={"items": [line]}))
    assert out["items"][0]["rate"] == "1000.01" and out["items"][0]["amount"] == "3000.03"
    assert out["assessed_amount"] == "3000.03"
    neg = call(client, "put", ANITA, url, json={"items": [{**line, "quantity": "-1"}]})
    assert neg.json()["error"]["code"] == "NEGATIVE_LINE"                          # only omissions go negative
    star = call(client, "put", ANITA, url, json={"items": [{**line, "pricing_basis": "STAR_RATE"}]})
    assert star.json()["error"]["code"] == "RATE_REFERENCE_REQUIRED"


def test_segregation_of_duties_through_approval(client, world):
    v = priced(client, world)
    r = call(client, "post", ANITA, f"/variations/{v['id']}/recommend", json={})
    assert r.status_code == 403                                     # not a commercial manager (and the assessor)
    assert call(client, "post", KABIR, f"/variations/{v['id']}/recommend",
                json={"amount": "1.00", "comment": "x"}).json()["error"]["code"] == "REASON_REQUIRED"
    ok(call(client, "post", KABIR, f"/variations/{v['id']}/recommend", json={}))
    assert call(client, "post", KABIR, f"/variations/{v['id']}/decision",
                json={"decision": "APPROVE"}).status_code == 403
    r = call(client, "post", MEERA, f"/variations/{v['id']}/decision", json={"decision": "APPROVE", "amount": "200000"})
    assert r.json()["error"]["code"] == "REASON_REQUIRED"
    ok(call(client, "post", MEERA, f"/variations/{v['id']}/decision", json={"decision": "RETURN", "reason": "Check valve size"}))
    got = ok(call(client, "get", MEERA, f"/variations/{v['id']}"))
    assert got["status"] == "COMMERCIAL_REVIEW" and got["recommended_by"] is None


def test_approver_who_raised_cannot_decide(client, world):
    """A contracts approver who also raised the variation (via a second role) still cannot approve it."""
    ok(call(client, "post", "priya.coord", f"/projects/{world['project_id']}/members",
            json={"username": MEERA, "roles": ["contracts_approver", "project_controls"]}), 201)
    v = ok(call(client, "post", MEERA, f"/subcontracts/{world['sc1']}/variations", json={
        "title": "Acceleration of PR-4 erection", "category": "ACCELERATION", "event_date": day(1),
        "description": "Second shift requested to recover the PR-4 milestone."}), 201)
    full = ok(call(client, "get", MEERA, f"/variations/{v['id']}"))
    assert "decide" not in full["actions"]
    from app.db import SessionLocal
    from app.models import Variation
    with SessionLocal() as db:
        db.get(Variation, v["id"]).status = "APPROVAL_PENDING"
        db.commit()
    r = call(client, "post", MEERA, f"/variations/{v['id']}/decision", json={"decision": "APPROVE", "amount": "1"})
    assert r.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"


# ---------------------------------------------------------------- state machine and access
def test_state_machine_and_locks(client, world):
    v = raise_var(client, world, category=None)
    assert to(client, v["id"], "APPROVED").json()["error"]["code"] == "INVALID_TRANSITION"
    assert to(client, v["id"], "CLASSIFIED").json()["error"]["code"] == "INCOMPLETE"
    assert to(client, v["id"], "WITHDRAWN").json()["error"]["code"] == "REASON_REQUIRED"
    e = to_valuation(client, world)
    assert to(client, e["id"], "COMMERCIAL_REVIEW").json()["error"]["code"] == "NOT_VALUED"
    p = pending(client, world)
    r = call(client, "patch", ANITA, f"/variations/{p['id']}", json={"claimed_amount": "9"})
    assert r.json()["error"]["code"] == "LOCKED"
    # not entitled -> can only be rejected, with reasons
    n = raise_var(client, world)
    ok(to(client, n["id"], "CLASSIFIED"))
    ok(call(client, "patch", ANITA, f"/variations/{n['id']}",
            json={"notice_given_on": day(1), "notice_revision_id": notice_doc(client, world)}))
    ok(to(client, n["id"], "NOTICE_REVIEW"))
    ok(to(client, n["id"], "ENTITLEMENT_ASSESSMENT"))
    assert to(client, n["id"], "REJECTED", reason="no").json()["error"]["code"] == "ENTITLEMENT_NOT_REFUSED"
    ok(call(client, "post", ANITA, f"/variations/{n['id']}/entitlement",
            json={"status": "NOT_ENTITLED", "basis": "Valves are shown on the tender P&ID; within scope."}))
    assert to(client, n["id"], "COST_AND_TIME_ASSESSMENT").json()["error"]["code"] == "NO_ENTITLEMENT"
    assert ok(to(client, n["id"], "REJECTED", reason="Within original scope (tender P&ID rev 2)"))["status"] == "REJECTED"


def test_raise_rules_access_and_idempotency(client, world):
    r = call(client, "post", ANITA, f"/subcontracts/{world['sc2']}/variations",
             json={"title": "Draft subcontract change", "event_date": day(1)})
    assert r.json()["error"]["code"] == "SUBCONTRACT_NOT_ACTIVE"
    assert call(client, "post", "dev.audit", f"/subcontracts/{world['sc1']}/variations",
                json={"title": "Auditor raising", "event_date": day(1)}).status_code == 403
    v = raise_var(client, world)
    assert call(client, "get", "omar.solar", f"/variations/{v['id']}").status_code == 403       # other project
    assert call(client, "get", "dev.audit", f"/variations/{v['id']}").status_code == 200
    key = {**H(ANITA), "Idempotency-Key": "var-test-1"}
    body = {"title": "Idempotent raise test", "event_date": day(1)}
    a = client.post(f"/api/v1/subcontracts/{world['sc1']}/variations", headers=key, json=body).json()
    b = client.post(f"/api/v1/subcontracts/{world['sc1']}/variations", headers=key, json=body).json()
    assert a["id"] == b["id"] and a["number"] == b["number"]
    c = client.post(f"/api/v1/subcontracts/{world['sc1']}/variations", headers=key, json={**body, "title": "Other one"})
    assert c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_events_link_to_variations_and_trace(client, world):
    pid = world["project_id"]
    ev = ok(call(client, "post", "arjun.qaqc", f"/projects/{pid}/events", json={
        "event_type": "SITE_CONDITION", "occurred_on": day(5), "description": "Rock encountered at foundation F-12",
        "subcontract_id": world["sc1"]}), 201)
    assert call(client, "post", "dev.audit", f"/projects/{pid}/events", json={
        "event_type": "OTHER", "occurred_on": day(1), "description": "Auditor cannot record"}).status_code == 403
    v = raise_var(client, world, origin_event_id=ev["id"], event_date=None)
    assert v["notice"]["clock_start"] == ev["occurred_on"]
    listed = ok(call(client, "get", ANITA, f"/projects/{pid}/events"))
    assert any(e["id"] == ev["id"] and e["variations"] == [v["number"]] for e in listed)
    demo = {x["number"]: x for x in ok(call(client, "get", ANITA, f"/projects/{pid}/variations"))["variations"]}
    t = ok(call(client, "get", "dev.audit", f"/variations/{demo['VAR-001']['id']}/trace"))
    kinds = {n["kind"] for n in t["nodes"]}
    assert {"ProjectEvent", "Obligation", "SourceClause", "Evidence", "Approval", "Amendment"} <= kinds
    assert {"MAY_GIVE_RISE_TO", "NOTICE_GOVERNED_BY", "FLOWS_DOWN_AS", "AFFECTS"} <= {e["type"] for e in t["edges"]}
