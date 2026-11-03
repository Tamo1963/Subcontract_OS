"""Directory group -> role mapping (ADR-0006). Provider traffic goes to the in-process mock IdP."""
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import SessionLocal
from app.idp import oidc
from app.models import DirectoryRoleGrant, User
from seed.mock_oidc import PEOPLE, MockIdP

ADMIN = {"X-User": "sys.admin"}
ANITA, VIKRAM, NEWHIRE, OUTSIDER, _ = PEOPLE
ISSUER = "https://idp.demo-epc.example"


def adm(client):
    """A separate administrator 'browser', so the signed-in user's cookie never leaks into admin calls."""
    return TestClient(client.app, headers=ADMIN)


@pytest.fixture
def idp(client, monkeypatch):
    m = MockIdP(issuer=ISSUER)
    monkeypatch.setattr(oidc, "TRANSPORT", httpx.MockTransport(m.handle))
    oidc._discovery.clear()
    oidc._jwks.clear()
    return m


def provider(client, **kw):
    body = {"name": "Company SSO", "provider_type": "okta", "issuer": ISSUER, "client_id": "scos-demo",
            "client_secret": "demo-secret", "allowed_domains": ["demo-epc.example"], "auto_provision": True,
            "sync_roles": True, **kw}
    r = adm(client).post("/api/v1/auth/identity-providers", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def mapping(client, pid, group, roles, project_id=None, label=""):
    r = adm(client).post(f"/api/v1/auth/identity-providers/{pid}/group-mappings",
                    json={"group_value": group, "label": label or group, "roles": roles, "project_id": project_id})
    assert r.status_code == 201, r.text
    return r.json()


def sso(client, idp, pid, person):
    client.cookies.clear()
    start = client.get(f"/api/v1/auth/oidc/{pid}/start", follow_redirects=False)
    _, code, state = idp.authorize(start.headers["location"], person)
    r = client.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.headers["location"] in ("/", "/#linked"), r.headers["location"]
    return client.get("/api/v1/me").json()


def test_mapping_admin_rules(client, world, idp):
    p = provider(client)
    url = f"/api/v1/auth/identity-providers/{p['id']}/group-mappings"
    assert client.post(url, headers={"X-User": "anita.contracts"},
                       json={"group_value": "G", "roles": ["auditor"]}).status_code == 403
    r = adm(client).post(url, json={"group_value": "IT-Admins", "roles": ["system_admin"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ROLE_NOT_DELEGABLE"
    assert adm(client).post(url, json={"group_value": "G", "roles": ["wizard"]}).status_code == 422
    assert adm(client).post(url, json={"group_value": "G", "roles": ["auditor"],
                                                 "project_id": "no-such-project"}).status_code == 422
    mapping(client, p["id"], "EPC-Legal", ["legal_reviewer"], world["project_id"])
    dup = adm(client).post(url, json={"group_value": "epc-legal", "roles": ["auditor"],
                                                "project_id": world["project_id"]})
    assert dup.status_code == 409
    listing = adm(client).get(url).json()
    assert "system_admin" not in listing["assignable_roles"] and listing["mappings"][0]["scope"] == "P-101"


def test_project_scoped_group_gives_project_access(client, world, idp):
    p = provider(client)
    mapping(client, p["id"], "P101-Project-Team", ["auditor"], world["project_id"])
    who = sso(client, idp, p["id"], NEWHIRE)              # auto-created, no manual roles at all
    assert who["username"] == "new.engineer" and who["projects"] == {world["project_id"]: ["auditor"]}
    assert [x["project_code"] for x in client.get("/api/v1/projects").json()] == ["P-101"]
    assert client.get(f"/api/v1/audit-events?project_id={world['project_id']}").status_code == 200   # auditor can
    assert client.get(f"/api/v1/projects/{world['p202']}").status_code == 403                       # other project no
    members = client.get(f"/api/v1/projects/{world['project_id']}/members").json()
    assert any(m["username"] == "new.engineer" and m["directory_roles"] == ["auditor"] for m in members)


def test_global_group_role(client, idp):
    p = provider(client)
    mapping(client, p["id"], "EPC-Engineering", ["registry_manager"])
    who = sso(client, idp, p["id"], NEWHIRE)
    assert who["roles"] == ["registry_manager"] and who["manual_roles"] == []
    r = client.post("/api/v1/subcontractors", headers={"X-SCOS-CSRF": "1"}, json={
        "legal_name": "Coastal Insulation Pvt Ltd", "registration_number": "U74999MH2015PTC260001", "country_code": "IN"})
    assert r.status_code == 201


def test_leaving_a_group_withdraws_only_directory_roles(client, world, idp):
    p = provider(client)
    mapping(client, p["id"], "P101-Project-Team", ["auditor"], world["project_id"])
    with SessionLocal() as db:
        db.query(User).filter_by(username="anita.contracts").one().email = "anita.desai@demo-epc.example"
        db.commit()
    who = sso(client, idp, p["id"], ANITA)
    assert who["projects"][world["project_id"]] == ["auditor", "contracts_manager"]
    idp.overrides = {"claims": {"groups": ["EPC-Contracts"]}}          # removed from P101-Project-Team in the directory
    who = sso(client, idp, p["id"], ANITA)
    assert who["projects"][world["project_id"]] == ["contracts_manager"]   # manual role untouched
    actions = [e for e in adm(client).get("/api/v1/users/audit").json()]
    assert any(e["action"] == "auth.login" for e in actions)


def test_missing_or_overage_groups_claim_changes_nothing(client, world, idp):
    p = provider(client)
    mapping(client, p["id"], "P101-Project-Team", ["auditor"], world["project_id"])
    assert world["project_id"] in sso(client, idp, p["id"], NEWHIRE)["projects"]
    for override in ({"groups": None}, {"groups": None, "_claim_names": {"groups": "src1"}}):
        idp.overrides = {"claims": override}
        assert world["project_id"] in sso(client, idp, p["id"], NEWHIRE)["projects"], override
    with SessionLocal() as db:
        from app.models import AuditEvent
        assert db.query(AuditEvent).filter_by(action="auth.directory_groups_unavailable").count() == 2


def test_disabled_provider_removed_mapping_and_expiry_switch_roles_off(client, world, idp):
    p = provider(client, grant_ttl_days=30)
    m = mapping(client, p["id"], "P101-Project-Team", ["auditor"], world["project_id"])
    sso(client, idp, p["id"], NEWHIRE)
    assert client.get("/api/v1/me").json()["projects"]
    # 1. provider disabled -> directory roles stop counting at once (session still valid, access gone)
    adm(client).patch(f"/api/v1/auth/identity-providers/{p['id']}", json={"enabled": False})
    assert client.get("/api/v1/me").json()["projects"] == {}
    adm(client).patch(f"/api/v1/auth/identity-providers/{p['id']}", json={"enabled": True})
    assert client.get("/api/v1/me").json()["projects"]
    # 2. grant not refreshed by a sign-in within 30 days -> expires
    with SessionLocal() as db:
        for g in db.query(DirectoryRoleGrant).all():
            g.refreshed_at = auth.utcnow() - timedelta(days=31)
        db.commit()
    assert client.get("/api/v1/me").json()["projects"] == {}
    sso(client, idp, p["id"], NEWHIRE)                                      # signing in refreshes it
    assert client.get("/api/v1/me").json()["projects"]
    # 3. mapping removed -> withdrawn immediately
    r = adm(client).delete(f"/api/v1/auth/group-mappings/{m['id']}").json()
    assert r["grants_withdrawn"] == 1 and client.get("/api/v1/me").json()["projects"] == {}


def test_sync_off_grants_nothing(client, world, idp):
    p = provider(client, sync_roles=False)
    mapping(client, p["id"], "P101-Project-Team", ["auditor"], world["project_id"])
    assert sso(client, idp, p["id"], NEWHIRE)["projects"] == {}


def test_matching_is_case_insensitive_and_supports_paths_and_nested_claims(client, world, idp):
    p = provider(client, groups_claim="realm_access.roles")
    mapping(client, p["id"], "/epc-engineering", ["registry_manager"])
    idp.overrides = {"claims": {"groups": None, "realm_access": {"roles": ["EPC-ENGINEERING", "offline_access"]}}}
    assert sso(client, idp, p["id"], NEWHIRE)["roles"] == ["registry_manager"]


def test_preview_dry_run(client, world, idp):
    p = provider(client)
    mapping(client, p["id"], "P101-Project-Team", ["auditor"], world["project_id"])
    mapping(client, p["id"], "EPC-Contracts", ["commercial_manager"], world["project_id"])
    r = adm(client).post(f"/api/v1/auth/identity-providers/{p['id']}/group-mappings/preview",
                    json={"groups": ["epc-contracts", "Unrelated"]}).json()["result"]
    assert r == [{"group": "EPC-Contracts", "role": "commercial_manager", "scope": "P-101"}]


def test_segregation_of_duties_still_applies_to_directory_roles(client, world, idp):
    """Anita drafted SC-101-02; a directory group making her an approver must not let her approve it."""
    p = provider(client)
    mapping(client, p["id"], "EPC-Contracts", ["contracts_approver"], world["project_id"])
    with SessionLocal() as db:
        db.query(User).filter_by(username="anita.contracts").one().email = "anita.desai@demo-epc.example"
        db.commit()
    sso(client, idp, p["id"], ANITA)
    r = client.post(f"/api/v1/subcontracts/{world['sc2']}/transitions", headers={"X-SCOS-CSRF": "1"},
                    json={"target": "APPROVED_FOR_EXECUTION"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"


def test_admin_sees_directory_roles_with_source(client, world, idp):
    p = provider(client)
    mapping(client, p["id"], "P101-Project-Team", ["auditor"], world["project_id"], label="P-101 project team")
    sso(client, idp, p["id"], NEWHIRE)
    rows = adm(client).get("/api/v1/users/new.engineer/directory-roles").json()
    assert rows == [{**rows[0], "role": "auditor", "scope": "P-101", "group": "P-101 project team",
                     "provider": "Company SSO", "active": True}]
    listed = [u for u in adm(client).get("/api/v1/users").json()["users"] if u["username"] == "new.engineer"]
    assert listed[0]["directory_roles"] == ["auditor (project)"] and listed[0]["roles"] == []
