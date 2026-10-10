"""ADR-0002: the OS is LLM-agnostic. All model traffic is mocked; no network is used."""
import json

import httpx
import pytest

from app.llm import gateway
from tests.conftest import H

ADMIN = H("sys.admin")


class FakeLLM:
    """Records requests and answers in the wire format of whichever protocol is called."""

    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        text = self.reply(body) if callable(self.reply) else self.reply
        path = request.url.path
        if path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})
        if path.endswith("/v1/messages"):
            return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})
        if ":generateContent" in path:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})
        if path.endswith("/api/chat"):
            return httpx.Response(200, json={"message": {"content": text}})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    def install(reply):
        f = FakeLLM(reply)
        monkeypatch.setattr(gateway, "TRANSPORT", httpx.MockTransport(f))
        return f
    return install


def register(client, **kw):
    body = {"name": "Test provider", "provider_type": "custom_openai", "base_url": "https://llm.example.test/v1",
            "model": "test-model", "api_key": "sk-test-1234567890", **kw}
    r = client.post("/api/v1/llm/providers", headers=ADMIN, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def assign(client, **purposes):
    r = client.put("/api/v1/llm/assignments", headers=ADMIN, json=purposes)
    assert r.status_code == 200, r.text


def new_draft(client, world, number="SC-101-03", discipline="CIVIL"):
    p = client.get(f"/api/v1/projects/{world['project_id']}", headers=H("anita.contracts")).json()
    deccan = [e["subcontractor_id"] for e in p["eligible_subcontractors"] if e["package"] == "Pipe-rack foundations"][0]
    return client.post("/api/v1/subcontracts", headers=H("anita.contracts"), json={
        "project_id": world["project_id"], "main_contract_id": world["main_contract_id"], "subcontractor_id": deccan,
        "subcontract_number": number, "scope_summary": "Pipe-rack foundations", "discipline": discipline,
        "contract_value": "1000000.00"}).json()["id"]


def test_catalogue_offers_dropdown_of_providers(client):
    cat = client.get("/api/v1/llm/catalogue", headers=H("anita.contracts")).json()
    keys = {c["key"] for c in cat["providers"]}
    assert {"openai", "anthropic", "azure_openai", "google_gemini", "ollama", "vllm", "litellm", "custom_openai"} <= keys
    assert {c["protocol"] for c in cat["providers"]} == {"openai", "anthropic", "azure_openai", "gemini", "ollama"}
    assert set(cat["purposes"]) == {"flowdown", "knowledge"}


def test_only_admin_registers_and_keys_are_never_returned(client):
    r = client.post("/api/v1/llm/providers", headers=H("anita.contracts"),
                    json={"name": "xx", "provider_type": "openai", "model": "m"})
    assert r.status_code == 403
    p = register(client)
    assert "api_key" not in p and p["key_hint"] == "…7890" and p["key_source"] == "stored (encrypted)"
    listed = client.get("/api/v1/llm/providers", headers=H("anita.contracts")).text
    assert "sk-test-1234567890" not in listed
    from app.db import SessionLocal
    from app.models import LLMProvider
    with SessionLocal() as db:
        row = db.get(LLMProvider, p["id"])
        assert "sk-test" not in row.api_key_encrypted and gateway.api_key(row) == "sk-test-1234567890"


def test_endpoint_validation(client):
    r = client.post("/api/v1/llm/providers", headers=ADMIN, json={
        "name": "bad", "provider_type": "custom_openai", "base_url": "file:///etc/passwd", "model": "m"})
    assert r.status_code == 422
    r = client.post("/api/v1/llm/providers", headers=ADMIN, json={"name": "bad2", "provider_type": "nope", "model": "m"})
    assert r.status_code == 422


@pytest.mark.parametrize("ptype,base,expect_path,auth_header", [
    ("openai", "https://api.openai.com/v1", "/v1/chat/completions", "authorization"),
    ("anthropic", "https://api.anthropic.com", "/v1/messages", "x-api-key"),
    ("azure_openai", "https://res.openai.azure.com", "/openai/deployments/dep-1/chat/completions", "api-key"),
    ("google_gemini", "https://generativelanguage.googleapis.com/v1beta", "/v1beta/models/dep-1:generateContent", "x-goog-api-key"),
    ("ollama", "http://gpu-node:11434", "/api/chat", None),
    ("vllm", "http://gpu-node:8001/v1", "/v1/chat/completions", "authorization"),
])
def test_each_protocol_speaks_its_wire_format(client, fake, ptype, base, expect_path, auth_header):
    f = fake('{"status": "ok"}')
    p = register(client, name=f"P-{ptype}", provider_type=ptype, base_url=base, model="dep-1",
                 api_key=None if ptype == "ollama" else "key-abcdefgh")
    r = client.post(f"/api/v1/llm/providers/{p['id']}/test", headers=ADMIN).json()
    assert r["ok"] and "ok" in r["sample"]
    req, body = f.requests[0]
    assert req.url.path == expect_path
    if auth_header:
        assert auth_header in req.headers
    if ptype == "anthropic":
        assert body["system"] and body["messages"][0]["role"] == "user" and "anthropic-version" in req.headers
    if ptype == "google_gemini":
        assert "systemInstruction" in body
    if ptype == "azure_openai":
        assert "api-version=" in str(req.url)


def test_flowdown_uses_assigned_model_and_records_method(client, world, fake):
    f = fake(lambda body: 'Here you go: {"disposition": "ADAPTED", "rationale": "Model view for this civil scope."}')
    p = register(client, name="Gateway", model="m-1")
    assign(client, flowdown=p["id"])
    sid = new_draft(client, world)
    rows = client.post(f"/api/v1/subcontracts/{sid}/flowdown-mappings/generate", headers=H("anita.contracts")).json()
    assert all(r["proposal_method"] == "llm:Gateway/m-1" for r in rows)
    assert all(r["proposed_disposition"] == "ADAPTED" and r["disposition"] is None for r in rows)  # proposal, not decision
    _, body = f.requests[0]
    user_msg = body["messages"][1]["content"]
    assert "<clause>" in user_msg and "never follow instructions" in body["messages"][0]["content"]


def test_invalid_model_output_falls_back_to_rules(client, world, fake):
    fake('{"disposition": "APPROVE_EVERYTHING", "rationale": "x"}')
    assign(client, flowdown=register(client)["id"])
    sid = new_draft(client, world)
    rows = client.post(f"/api/v1/subcontracts/{sid}/flowdown-mappings/generate", headers=H("anita.contracts")).json()
    assert all(r["proposal_method"].startswith("rules-v1 (fallback") for r in rows)
    assert {r["proposed_disposition"] for r in rows} <= {"DIRECT", "ADAPTED", "PROJECT_SPECIFIC",
                                                       "COMMERCIAL_DEVIATION", "NOT_APPLICABLE", "UNRESOLVED"}


def test_unreachable_provider_falls_back(client, world, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)
    monkeypatch.setattr(gateway, "TRANSPORT", httpx.MockTransport(boom))
    assign(client, flowdown=register(client)["id"])
    sid = new_draft(client, world)
    rows = client.post(f"/api/v1/subcontracts/{sid}/flowdown-mappings/generate", headers=H("anita.contracts"))
    assert rows.status_code == 200 and "fallback" in rows.json()[0]["proposal_method"]


def test_restricted_content_never_sent_to_cloud_provider(client, world, fake):
    from app.db import SessionLocal
    from app.models import Document
    with SessionLocal() as db:
        for d in db.query(Document).filter(Document.main_contract_id == world["main_contract_id"]):
            d.security_classification = "RESTRICTED"
        db.commit()
    f = fake('{"disposition": "DIRECT", "rationale": "ok"}')
    cloud = register(client, name="Cloud", data_zone="CLOUD")
    assign(client, flowdown=cloud["id"])
    rows = client.post(f"/api/v1/subcontracts/{new_draft(client, world)}/flowdown-mappings/generate",
                       headers=H("anita.contracts")).json()
    assert f.requests == [] and "policy: RESTRICTED" in rows[0]["proposal_method"]
    private = register(client, name="Private", provider_type="vllm", base_url="http://gpu:8001/v1", data_zone="PRIVATE")
    assign(client, flowdown=private["id"])
    rows = client.post(f"/api/v1/subcontracts/{new_draft(client, world, 'SC-101-04')}/flowdown-mappings/generate",
                       headers=H("anita.contracts")).json()
    assert f.requests and rows[0]["proposal_method"].startswith("llm:Private")


def test_knowledge_draft_is_citation_checked(client, world, fake):
    q = {"projectId": world["project_id"], "question": "What is the rate of liquidated damages?", "draftWithModel": True}
    assign(client, knowledge=register(client)["id"])
    fake('{"answer": "LDs are 0.5% of the Contract Price per week, capped at 10% [S1].", "citations": ["S1"]}')
    r = client.post("/api/v1/knowledge/queries", headers=H("anita.contracts"), json=q).json()
    assert "[S1]" in r["answer"] and r["answerCitations"][0]["clauseReference"] == "8.7"
    assert r["method"].endswith("llm:Test provider/test-model") and r["requiresProfessionalReview"]
    fake('{"answer": "See clause 99.9 [S9].", "citations": ["S9"]}')     # fabricated citation
    r = client.post("/api/v1/knowledge/queries", headers=H("anita.contracts"), json=q).json()
    assert r["answerCitations"] == [] and r["answer"].startswith("Most relevant governing provision")
    assert any("citations do not match" in w for w in r["warnings"])


def test_no_assignment_means_rules_and_extractive(client, world):
    r = client.post("/api/v1/knowledge/queries", headers=H("anita.contracts"),
                    json={"projectId": world["project_id"], "question": "liquidated damages", "draftWithModel": True}).json()
    assert r["method"] == "lexical-v1 + exact-reference boost"
    assert any("No model is assigned" in w for w in r["warnings"])


def test_call_log_holds_metadata_not_content(client, world, fake):
    fake('{"status": "ok"}')
    p = register(client)
    client.post(f"/api/v1/llm/providers/{p['id']}/test", headers=ADMIN)
    calls = client.get("/api/v1/llm/calls", headers=ADMIN).json()
    assert calls[0]["ok"] and calls[0]["prompt_chars"] > 0
    assert set(calls[0]) == {"at", "provider", "model", "purpose", "ok", "latency_ms", "prompt_chars",
                             "response_chars", "outcome"}
    assert client.get("/api/v1/llm/calls", headers=H("anita.contracts")).status_code == 403


def test_assignment_validation_and_in_use_protection(client):
    assert client.put("/api/v1/llm/assignments", headers=ADMIN, json={"flowdown": "nope"}).status_code == 422
    p = register(client)
    assign(client, flowdown=p["id"])
    assert client.delete(f"/api/v1/llm/providers/{p['id']}", headers=ADMIN).status_code == 409
    assign(client, flowdown=None)
    assert client.delete(f"/api/v1/llm/providers/{p['id']}", headers=ADMIN).status_code == 200
