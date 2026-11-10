"""Measurement, payment advice, approval and accounts' receipts (blueprint 5.4; PAY-01/02/03; ADR-0010)."""
from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import H

KABIR, ROHAN, MEERA, LATA = "kabir.commercial", "rohan.site", "meera.approver", "lata.accounts"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def day(n):
    return (date.today() + timedelta(days=n)).isoformat()


def call(client, method, who, url, **kw):
    return getattr(client, method)(f"/api/v1{url}", headers=H(who), **kw)


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def overview(client, world, who=KABIR, sc="sc1"):
    return ok(call(client, "get", who, f"/subcontracts/{world[sc]}/payments"))


def advice(client, world, number):
    return next(a for a in overview(client, world)["advices"] if a["number"] == number)


def mb_doc(client, world, number="MB-SC-101-01-003"):
    docs = ok(call(client, "get", KABIR, f"/projects/{world['project_id']}/documents"))
    return next(d for d in docs if d["document_number"] == number)["revisions"][0]["id"]


def new_measurement(client, world, qty="100", rate="640.50", who=KABIR, verify=True):
    m = ok(call(client, "post", who, f"/subcontracts/{world['sc1']}/measurements", json={
        "period_from": day(-2), "period_to": day(0), "evidence_revision_id": mb_doc(client, world),
        "lines": [{"boq_ref": "3.01", "description": "Pipe erection", "unit": "m", "quantity": qty, "rate": rate}]}), 201)
    if verify:
        ok(call(client, "post", ROHAN, f"/measurements/{m['id']}/verify", json={"decision": "VERIFY"}))
    return m


def approved_advice(client, world, **kw):
    m = new_measurement(client, world, **kw)
    a = ok(call(client, "post", KABIR, f"/subcontracts/{world['sc1']}/payment-advices",
                json={"measurement_ids": [m["id"]]}), 201)
    ok(call(client, "post", KABIR, f"/payment-advices/{a['id']}/decision", json={"action": "SUBMIT"}))
    return ok(call(client, "post", MEERA, f"/payment-advices/{a['id']}/decision", json={"action": "APPROVE"}))


def pay(client, aid, amount, ref, paid_on=None, content=PNG, name="receipt.png", who=LATA):
    return call(client, "post", who, f"/payment-advices/{aid}/payments", files={"receipt": (name, content, "image/png")},
                data={"paid_on": paid_on or day(0), "amount": amount, "mode": "NEFT", "payment_reference": ref})


# ---------------------------------------------------------------- PAY-01
def test_PAY01_certificate_recalculates_from_its_inputs(client, world):
    pa3 = advice(client, world, "PA-003")
    full = ok(call(client, "get", KABIR, f"/payment-advices/{pa3['id']}"))
    work = Decimal("4845000.00") + Decimal("2305800.00") + Decimal("222000.00") + Decimal("640000.00")  # MB-002 + VAR-001
    assert Decimal(full["work_value"]) == work
    expect = work + work * Decimal("0.18") - work * Decimal("0.05") - work * Decimal("0.10") - work * Decimal("0.02") \
        - Decimal("85000.00")
    assert Decimal(full["net_payable"]) == expect.quantize(Decimal("0.01"))
    r = ok(call(client, "get", "dev.audit", f"/payment-advices/{pa3['id']}/recalculate"))
    assert r["reconciles"] is True and r["recomputed"]["net_payable"] == full["net_payable"]
    assert full["terms"]["version"] == 1 and full["calculation"]["to_date"]["advices_counted"] == ["PA-001", "PA-002"]


def test_PAY01_tampered_figures_are_detected_and_cannot_be_approved(client, world):
    m = new_measurement(client, world)
    a = ok(call(client, "post", KABIR, f"/subcontracts/{world['sc1']}/payment-advices", json={"measurement_ids": [m["id"]]}), 201)
    from app.db import SessionLocal
    from app.models import PaymentAdvice
    with SessionLocal() as db:
        db.get(PaymentAdvice, a["id"]).net_payable = Decimal("99999999.00")
        db.commit()
    r = ok(call(client, "get", KABIR, f"/payment-advices/{a['id']}/recalculate"))
    assert r["reconciles"] is False and "net_payable" in r["differences"]
    ok(call(client, "post", KABIR, f"/payment-advices/{a['id']}/decision", json={"action": "SUBMIT"}))
    r = call(client, "post", MEERA, f"/payment-advices/{a['id']}/decision", json={"action": "APPROVE"})
    assert r.json()["error"]["code"] == "DOES_NOT_RECONCILE"


def test_retention_cap_and_advance_recovery_limits(client, world):
    sid = world["sc1"]
    terms = {"retention_percent": "5", "retention_cap_percent": "0.6", "advance_amount": "12500000.00",
             "advance_recovery_percent": "90", "gst_percent": "18", "tds_percent": "2",
             "source_reference": "SC-101-01 cl. 14"}
    r = call(client, "put", KABIR, f"/subcontracts/{sid}/payment-terms", json=terms)
    assert r.json()["error"]["code"] == "REASON_REQUIRED"                 # payments approved under current terms
    assert ok(call(client, "put", KABIR, f"/subcontracts/{sid}/payment-terms",
                   json={**terms, "reason": "Test: Amendment 3 changes payment terms"}))["version"] == 2
    a = approved_advice(client, world, qty="20000", rate="640.50")            # work 12,810,000
    cap = (Decimal("127490000.00") * Decimal("0.006")).quantize(Decimal("0.01"))
    held = Decimal("689810.00")
    assert Decimal(a["retention_amount"]) == cap - held                      # only the room left under the cap
    outstanding = Decimal("12500000.00") - Decimal("1379620.00")
    assert Decimal(a["advance_recovery"]) == outstanding                     # never more than is outstanding
    assert call(client, "put", KABIR, f"/subcontracts/{sid}/payment-terms",
                json={**terms, "gst_percent": "180", "reason": "x" * 12}).json()["error"]["code"] == "INVALID_PERCENT"


# ---------------------------------------------------------------- PAY-02
def test_PAY02_same_idempotency_key_creates_one_advice(client, world):
    m = new_measurement(client, world)
    key = {**H(KABIR), "Idempotency-Key": "pay-test-1"}
    url = f"/api/v1/subcontracts/{world['sc1']}/payment-advices"
    a = client.post(url, headers=key, json={"measurement_ids": [m["id"]]}).json()
    b = client.post(url, headers=key, json={"measurement_ids": [m["id"]]}).json()
    assert a["id"] == b["id"] and [x["number"] for x in overview(client, world)["advices"]].count(a["number"]) == 1
    c = client.post(url, headers=key, json={"measurement_ids": [m["id"]], "advance_amount": "1"})
    assert c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ---------------------------------------------------------------- PAY-03 and segregation
def test_PAY03_preparer_cannot_approve_and_approver_cannot_record_payment(client, world):
    ok(call(client, "post", "priya.coord", f"/projects/{world['project_id']}/members",
            json={"username": KABIR, "roles": ["commercial_manager", "contracts_approver"]}), 201)
    m = new_measurement(client, world)
    a = ok(call(client, "post", KABIR, f"/subcontracts/{world['sc1']}/payment-advices", json={"measurement_ids": [m["id"]]}), 201)
    ok(call(client, "post", KABIR, f"/payment-advices/{a['id']}/decision", json={"action": "SUBMIT"}))
    r = call(client, "post", KABIR, f"/payment-advices/{a['id']}/decision", json={"action": "APPROVE"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"
    ok(call(client, "post", MEERA, f"/payment-advices/{a['id']}/decision", json={"action": "APPROVE"}))
    assert pay(client, a["id"], "1", "UTR-TEST-0001", who=MEERA).status_code == 403          # not accounts
    ok(call(client, "post", "priya.coord", f"/projects/{world['project_id']}/members",
            json={"username": MEERA, "roles": ["contracts_approver", "accounts"]}), 201)
    assert pay(client, a["id"], "1", "UTR-TEST-0001", who=MEERA).json()["error"]["code"] == "SEGREGATION_OF_DUTIES"


def test_measurement_rules(client, world):
    mb3 = next(m for m in overview(client, world)["measurements"] if m["number"] == "MB-003")
    r = call(client, "post", KABIR, f"/subcontracts/{world['sc1']}/payment-advices", json={"measurement_ids": [mb3["id"]]})
    assert r.json()["error"]["code"] == "MEASUREMENT_NOT_VERIFIED"
    m = new_measurement(client, world, who=ROHAN, verify=False)
    r = call(client, "post", ROHAN, f"/measurements/{m['id']}/verify", json={"decision": "VERIFY"})
    assert r.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"
    assert call(client, "post", "arjun.qaqc", f"/measurements/{m['id']}/verify",
                json={"decision": "RETURN"}).json()["error"]["code"] == "REASON_REQUIRED"
    r = call(client, "post", KABIR, f"/subcontracts/{world['sc1']}/measurements", json={
        "period_from": day(0), "period_to": day(3), "evidence_revision_id": mb_doc(client, world),
        "lines": [{"boq_ref": "3.01", "description": "Pipe erection", "unit": "m", "quantity": "1", "rate": "1"}]})
    assert r.json()["error"]["code"] == "INVALID_PERIOD"                      # cannot measure the future
    r = call(client, "post", KABIR, f"/subcontracts/{world['sc3']}/measurements", json={
        "period_from": day(-1), "period_to": day(0), "evidence_revision_id": mb_doc(client, world),
        "lines": [{"boq_ref": "1", "description": "Cable laying", "unit": "m", "quantity": "1", "rate": "1"}]})
    assert r.json()["error"]["code"] == "SUBCONTRACT_NOT_ACTIVE"            # SC-101-03 is still mobilising


def test_nothing_is_paid_twice_and_cancelling_releases(client, world):
    o = overview(client, world)
    mb1 = next(m for m in o["measurements"] if m["number"] == "MB-001")
    url = f"/subcontracts/{world['sc1']}/payment-advices"
    assert mb1["advised"] and call(client, "post", KABIR, url, json={"measurement_ids": [mb1["id"]]}).json()["error"]["code"] \
        == "ALREADY_ADVISED"
    vs = {v["number"]: v for v in ok(call(client, "get", KABIR, f"/projects/{world['project_id']}/variations"))["variations"]}
    assert call(client, "post", KABIR, url, json={"variation_ids": [vs["VAR-001"]["id"]]}).json()["error"]["code"] == "ALREADY_ADVISED"
    assert call(client, "post", KABIR, url, json={"variation_ids": [vs["VAR-002"]["id"]]}).json()["error"]["code"] \
        == "VARIATION_NOT_INCORPORATED"
    assert call(client, "post", KABIR, url, json={"advance_amount": "1.00"}).json()["error"]["code"] == "ADVANCE_EXCEEDS_TERMS"
    m = new_measurement(client, world)
    a = ok(call(client, "post", KABIR, url, json={"measurement_ids": [m["id"]]}), 201)
    assert m["id"] not in [x["id"] for x in overview(client, world)["payable"]["measurements"]]
    assert call(client, "post", KABIR, f"/payment-advices/{a['id']}/decision",
                json={"action": "CANCEL"}).json()["error"]["code"] == "REASON_REQUIRED"
    ok(call(client, "post", KABIR, f"/payment-advices/{a['id']}/decision", json={"action": "CANCEL", "comment": "Wrong period"}))
    assert m["id"] in [x["id"] for x in overview(client, world)["payable"]["measurements"]]
    assert ok(call(client, "post", KABIR, url, json={"measurement_ids": [m["id"]]}), 201)["number"] != a["number"]


def test_deductions_need_evidence_and_net_cannot_go_negative(client, world):
    m = new_measurement(client, world, qty="10", rate="100")
    url = f"/subcontracts/{world['sc1']}/payment-advices"
    d = {"kind": "BACK_CHARGE", "description": "Rework of supports by EPC", "amount": "500.00"}
    assert call(client, "post", KABIR, url, json={"measurement_ids": [m["id"]], "deductions": [d]}).json()["error"]["code"] \
        == "EVIDENCE_REQUIRED"
    big = {**d, "kind": "OTHER", "amount": "999999.00"}
    assert call(client, "post", KABIR, url, json={"measurement_ids": [m["id"]], "deductions": [big]}).json()["error"]["code"] \
        == "NEGATIVE_NET"


def test_return_goes_back_to_draft(client, world):
    m = new_measurement(client, world)
    a = ok(call(client, "post", KABIR, f"/subcontracts/{world['sc1']}/payment-advices", json={"measurement_ids": [m["id"]]}), 201)
    ok(call(client, "post", KABIR, f"/payment-advices/{a['id']}/decision", json={"action": "SUBMIT"}))
    assert call(client, "post", MEERA, f"/payment-advices/{a['id']}/decision",
                json={"action": "RETURN"}).json()["error"]["code"] == "REASON_REQUIRED"
    out = ok(call(client, "post", MEERA, f"/payment-advices/{a['id']}/decision",
                  json={"action": "RETURN", "comment": "Attach the signed MB"}))
    assert out["status"] == "DRAFT" and any(h["action"] == "payment.advice_return" for h in out["history"])


# ---------------------------------------------------------------- accounts: payment outside, receipt uploaded
def test_accounts_records_payment_with_receipt(client, world):
    pa3 = advice(client, world, "PA-003")
    net = Decimal(pa3["net_payable"])
    q = ok(call(client, "get", LATA, "/payment-advices"))
    assert [a["number"] for a in q["advices"]] == ["PA-003"] and "record_payment" in q["advices"][0]["actions"]
    assert pay(client, pa3["id"], str(net + 1), "UTR-PA3-01").json()["error"]["code"] == "AMOUNT_EXCEEDS_BALANCE"
    assert pay(client, pa3["id"], "1000", "UTR-PA3-01", paid_on=day(1)).json()["error"]["code"] == "FUTURE_DATE"
    assert pay(client, pa3["id"], "1000", "UTR-PA3-01", paid_on=day(-40)).json()["error"]["code"] == "BEFORE_APPROVAL"
    assert pay(client, pa3["id"], "1000", "UTR-PA3-01", content=b"not an image").json()["error"]["code"] == "QUARANTINED"
    r = ok(pay(client, pa3["id"], "5000000.00", "UTR-PA3-01"), 201)
    assert r["status"] == "PART_PAID" and Decimal(r["balance"]) == net - Decimal("5000000.00")
    assert r["payments"][0]["receipt"]["document_number"] == "RCPT-SC-101-01-PA-003-1"
    assert pay(client, pa3["id"], "1", "UTR-PA3-01", content=PNG + b"x").json()["error"]["code"] == "DUPLICATE_REFERENCE"
    assert pay(client, pa3["id"], "1", "UTR-PA3-02").json()["error"]["code"] == "RECEIPT_ALREADY_USED"   # same file
    r = ok(pay(client, pa3["id"], r["balance"], "UTR-PA3-02", content=PNG + b"2"), 201)
    assert r["status"] == "PAID" and r["balance"] == "0.00"
    assert pay(client, pa3["id"], "1", "UTR-PA3-03", content=PNG + b"3").json()["error"]["code"] == "NOT_WITH_ACCOUNTS"
    docs = ok(call(client, "get", KABIR, f"/projects/{world['project_id']}/documents"))
    assert {"RCPT-SC-101-01-PA-003-1", "RCPT-SC-101-01-PA-003-2"} <= {d["document_number"] for d in docs}
    pos = overview(client, world)["position"]
    assert pos["with_accounts"] == "0.00" and Decimal(pos["paid"]) == Decimal(pos["net_approved"])


def test_print_trace_access_and_terms_required(client, world):
    pa3 = advice(client, world, "PA-003")
    page = call(client, "get", LATA, f"/payment-advices/{pa3['id']}/print")
    assert page.status_code == 200 and "Payment advice PA-003" in page.text and "80,07,928.00" in page.text \
        and "8,007,928" not in page.text and "Shakti Piping Works" in page.text and page.text.count("Net payable") == 1
    from app.routers.payments import indian
    assert [indian(Decimal(x)) for x in ("0", "999.5", "100000", "-12345678.9")] == \
        ["0.00", "999.50", "1,00,000.00", "-1,23,45,678.90"]
    t = ok(call(client, "get", "dev.audit", f"/payment-advices/{pa3['id']}/trace"))
    assert {"Measurement", "Variation", "Deduction", "Approval", "Evidence"} <= {n["kind"] for n in t["nodes"]}
    vs = {v["number"]: v for v in ok(call(client, "get", KABIR, f"/projects/{world['project_id']}/variations"))["variations"]}
    vt = ok(call(client, "get", "dev.audit", f"/variations/{vs['VAR-001']['id']}/trace"))
    assert any(n["kind"] == "PaymentAdvice" and n["label"].startswith("PA-003") for n in vt["nodes"])
    assert call(client, "get", "omar.solar", f"/subcontracts/{world['sc1']}/payments").status_code == 403
    assert call(client, "get", "omar.solar", f"/payment-advices/{pa3['id']}").status_code == 403
    assert ok(call(client, "get", "omar.solar", "/payment-advices"))["advices"] == []
    aud = overview(client, world, "dev.audit")
    assert all(a["actions"] == [] for a in aud["advices"]) and aud["can"] == {"terms": False, "measure": False, "prepare": False}
    r = call(client, "post", KABIR, f"/subcontracts/{world['sc3']}/payment-advices", json={"advance_amount": "1"})
    assert r.json()["error"]["code"] == "TERMS_NOT_SET"
    draft = ok(call(client, "post", KABIR, f"/subcontracts/{world['sc1']}/payment-advices",
                    json={"measurement_ids": [new_measurement(client, world)["id"]]}), 201)
    assert call(client, "get", KABIR, f"/payment-advices/{draft['id']}/print").status_code == 409
