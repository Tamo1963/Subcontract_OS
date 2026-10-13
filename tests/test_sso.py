"""Pluggable sign-in providers (OpenID Connect). All provider traffic goes to an in-process mock IdP."""
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth
from app.db import SessionLocal
from app.idp import oidc
from app.models import User
from seed.mock_oidc import PEOPLE, MockIdP

ADMIN = {"X-User": "sys.admin"}
CSRF = {"X-SCOS-CSRF": "1"}
ANITA, VIKRAM, NEWHIRE, OUTSIDER, UNVERIFIED = PEOPLE
ISSUER = "https://idp.demo-epc.example"


@pytest.fixture
def idp(client, monkeypatch):
    m = MockIdP(issuer=ISSUER)
    monkeypatch.setattr(oidc, "TRANSPORT", httpx.MockTransport(m.handle))
    oidc._discovery.clear()
    oidc._jwks.clear()
    return m


def register(client, **kw):
    body = {"name": "Company SSO", "provider_type": "okta", "issuer": ISSUER, "client_id": "scos-demo",
            "client_secret": "demo-secret", "allowed_domains": ["demo-epc.example"], **kw}
    r = client.post("/api/v1/auth/identity-providers", headers=ADMIN, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def set_email(username, email):
    with SessionLocal() as db:
        db.query(User).filter_by(username=username).one().email = email
        db.commit()


def sso(client, idp, pid, person, *, tamper_cookie=False):
    """Run the whole browser flow; returns the callback response (a redirect)."""
    start = client.get(f"/api/v1/auth/oidc/{pid}/start", follow_redirects=False)
    assert start.status_code == 303, start.text
    _, code, state = idp.authorize(start.headers["location"], person)
    if tamper_cookie:
        client.cookies.delete("scos_oidc", path="/api/v1/auth/oidc")
    return client.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False), state


def me(client):
    r = client.get("/api/v1/me")     # cookie only: no X-User header
    return r.json().get("username") if r.status_code == 200 else None


def test_provider_admin_is_admin_only_and_secret_never_returned(client, idp):
    assert client.get("/api/v1/auth/identity-providers", headers={"X-User": "anita.contracts"}).status_code == 403
    p = register(client)
    assert p["secret_source"] == "stored (encrypted)" and "demo-secret" not in str(p)
    listing = client.get("/api/v1/auth/identity-providers", headers=ADMIN).json()
    assert "demo-secret" not in str(listing) and listing["redirect_uri"].endswith("/api/v1/auth/oidc/callback")
    assert {"entra", "google", "okta", "keycloak", "generic"} <= {c["key"] for c in listing["catalogue"]}
    public = client.get("/api/v1/auth/providers").json()
    assert public["providers"][0]["label"] == "Sign in with Okta" and "issuer" not in str(public)


def test_issuer_validation(client, idp):
    for bad in ("http://idp.example.com", "https://login.microsoftonline.com/<tenant-id>/v2.0", "ftp://x"):
        r = client.post("/api/v1/auth/identity-providers", headers=ADMIN, json={
            "name": "bad " + bad[:5], "provider_type": "generic", "issuer": bad, "client_id": "c"})
        assert r.status_code == 422, bad


def test_connection_test(client, idp):
    p = register(client)
    r = client.post(f"/api/v1/auth/identity-providers/{p['id']}/test", headers=ADMIN).json()
    assert r["ok"] and r["signing_keys"] == 1 and r["pkce_s256"]


def test_invited_user_is_linked_by_verified_email_then_by_subject(client, world, idp):
    p = register(client)
    set_email("anita.contracts", "anita.desai@demo-epc.example")
    r, _ = sso(client, idp, p["id"], ANITA)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert me(client) == "anita.contracts"
    ids = client.get("/api/v1/me/identities").json()
    assert ids[0]["provider"] == "Company SSO"
    # Later sign-ins use the permanent subject, even if the e-mail at the provider changes
    client.cookies.clear()
    idp.overrides = {"claims": {"email": "a.desai@renamed.example"}}
    r, _ = sso(client, idp, p["id"], ANITA)
    assert r.headers["location"] == "/" and me(client) == "anita.contracts"


def test_session_from_sso_has_full_access_and_roles_stay_in_os(client, world, idp):
    p = register(client)
    set_email("anita.contracts", "anita.desai@demo-epc.example")
    sso(client, idp, p["id"], ANITA)
    projects = client.get("/api/v1/projects").json()
    assert [x["project_code"] for x in projects] == ["P-101"]           # project access from the OS, not the IdP


def test_invite_only_rejects_unknown_people(client, idp):
    p = register(client)
    r, _ = sso(client, idp, p["id"], NEWHIRE)
    assert r.headers["location"] == "/#signin-error=NO_ACCOUNT" and me(client) is None


def test_auto_provision_only_for_allowed_domains_and_without_roles(client, idp):
    p = register(client, auto_provision=True)
    r, _ = sso(client, idp, p["id"], NEWHIRE)
    assert r.headers["location"] == "/"
    who = client.get("/api/v1/me").json()
    assert who["username"] == "new.engineer" and who["roles"] == [] and who["projects"] == {}
    client.cookies.clear()
    r, _ = sso(client, idp, p["id"], OUTSIDER)
    assert r.headers["location"] == "/#signin-error=NO_ACCOUNT"


def test_unverified_email_never_matches_an_invitation(client, idp):
    p = register(client)
    set_email("anita.contracts", "anita.desai@demo-epc.example")
    r, _ = sso(client, idp, p["id"], UNVERIFIED)
    assert r.headers["location"] == "/#signin-error=NO_ACCOUNT" and me(client) is None


def test_state_is_one_time_and_bound_to_the_browser(client, idp):
    p = register(client, auto_provision=True)
    r, state = sso(client, idp, p["id"], NEWHIRE, tamper_cookie=True)
    assert r.headers["location"] == "/#signin-error=BROWSER_MISMATCH"
    replay = client.get("/api/v1/auth/oidc/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert replay.headers["location"] == "/#signin-error=INVALID_STATE"


@pytest.mark.parametrize("override,label", [
    ({"claims": {"nonce": "attacker-nonce"}}, "nonce"),
    ({"claims": {"aud": "some-other-app"}}, "audience"),
    ({"claims": {"iss": "https://evil.example"}}, "issuer"),
    ({"claims": {"exp": 1000000000}}, "expired"),
    ({"signing_key": rsa.generate_private_key(public_exponent=65537, key_size=2048)}, "forged signature"),
    ({"alg": "HS256"}, "symmetric algorithm"),
    ({"kid": "not-published"}, "unknown key"),
])
def test_tampered_id_tokens_are_rejected(client, idp, override, label):
    p = register(client, auto_provision=True)
    idp.overrides = override
    r, _ = sso(client, idp, p["id"], NEWHIRE)
    assert r.headers["location"].startswith("/#signin-error="), label
    assert r.headers["location"].split("=")[1] in ("INVALID_TOKEN", "UNKNOWN_SIGNING_KEY"), label
    assert me(client) is None, label


def test_wrong_client_secret_fails_token_exchange(client, idp):
    p = register(client, auto_provision=True, client_secret="wrong")
    r, _ = sso(client, idp, p["id"], NEWHIRE)
    assert r.headers["location"] == "/#signin-error=TOKEN_EXCHANGE_FAILED"


def test_signed_in_user_links_company_login(client, world, idp, monkeypatch):
    from app import config
    p = register(client)
    with SessionLocal() as db:
        u = db.query(User).filter_by(username="vikram.legal").one()
        u.password_hash, u.must_change_password = auth.hash_password("Legal-Review-2026"), False
        db.commit()
    monkeypatch.setattr(config, "ALLOW_DEV_AUTH", False)
    client.post("/api/v1/auth/login", json={"username": "vikram.legal", "password": "Legal-Review-2026"})
    assert client.post(f"/api/v1/auth/oidc/{p['id']}/link").status_code == 403          # CSRF header required
    url = client.post(f"/api/v1/auth/oidc/{p['id']}/link", headers=CSRF).json()["authorization_url"]
    _, code, state = idp.authorize(url, VIKRAM)
    r = client.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.headers["location"] == "/#linked"
    client.post("/api/v1/auth/logout", headers=CSRF)
    client.cookies.clear()
    r, _ = sso(client, idp, p["id"], VIKRAM)          # now signs in with no e-mail on file
    assert me(client) == "vikram.legal"


def test_one_identity_cannot_be_claimed_by_two_accounts(client, world, idp, monkeypatch):
    from app import config
    p = register(client)
    set_email("anita.contracts", "anita.desai@demo-epc.example")
    sso(client, idp, p["id"], ANITA)                   # Anita's subject now belongs to anita.contracts
    client.cookies.clear()
    with SessionLocal() as db:
        u = db.query(User).filter_by(username="dev.audit").one()
        u.password_hash, u.must_change_password = auth.hash_password("Audit-Review-2026"), False
        db.commit()
    monkeypatch.setattr(config, "ALLOW_DEV_AUTH", False)
    client.post("/api/v1/auth/login", json={"username": "dev.audit", "password": "Audit-Review-2026"})
    url = client.post(f"/api/v1/auth/oidc/{p['id']}/link", headers=CSRF).json()["authorization_url"]
    _, code, state = idp.authorize(url, ANITA)
    r = client.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.headers["location"] == "/#signin-error=ALREADY_LINKED"


def test_disabled_account_and_disabled_provider(client, world, idp):
    p = register(client)
    set_email("anita.contracts", "anita.desai@demo-epc.example")
    client.patch("/api/v1/users/anita.contracts", headers=ADMIN, json={"is_active": False})
    r, _ = sso(client, idp, p["id"], ANITA)
    assert r.headers["location"] == "/#signin-error=ACCOUNT_DISABLED"
    client.patch(f"/api/v1/auth/identity-providers/{p['id']}", headers=ADMIN, json={"enabled": False})
    start = client.get(f"/api/v1/auth/oidc/{p['id']}/start", follow_redirects=False)
    assert start.headers["location"] == "/#signin-error=PROVIDER_DISABLED"
    assert client.get("/api/v1/auth/providers").json()["providers"] == []


def test_password_login_can_be_limited_to_admins(client, idp, monkeypatch):
    from app import config
    assert client.put("/api/v1/auth/policy", headers=ADMIN, json={"password_login": "admins_only"}).status_code == 409
    register(client)
    assert client.put("/api/v1/auth/policy", headers=ADMIN, json={"password_login": "admins_only"}).status_code == 200
    for name in ("anita.contracts", "sys.admin"):
        with SessionLocal() as db:
            u = db.query(User).filter_by(username=name).one()
            u.password_hash, u.must_change_password = auth.hash_password("Strong-Pass-2026"), False
            db.commit()
    monkeypatch.setattr(config, "ALLOW_DEV_AUTH", False)
    r = client.post("/api/v1/auth/login", json={"username": "anita.contracts", "password": "Strong-Pass-2026"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "PASSWORD_LOGIN_DISABLED"
    assert client.post("/api/v1/auth/login", json={"username": "sys.admin", "password": "Strong-Pass-2026"}).status_code == 200
    assert client.get("/api/v1/auth/providers").json()["password_login"] == "admins_only"


def test_cannot_unlink_the_only_sign_in_method(client, idp):
    p = register(client, auto_provision=True)
    sso(client, idp, p["id"], NEWHIRE)                 # provisioned account has no password
    lid = client.get("/api/v1/me/identities").json()[0]["id"]
    r = client.delete(f"/api/v1/me/identities/{lid}", headers=CSRF)
    assert r.status_code == 409 and r.json()["error"]["code"] == "LAST_SIGN_IN_METHOD"


def test_sso_events_are_audited(client, world, idp):
    p = register(client)
    set_email("anita.contracts", "anita.desai@demo-epc.example")
    sso(client, idp, p["id"], ANITA)
    client.cookies.clear()
    sso(client, idp, p["id"], NEWHIRE)
    actions = [e["action"] for e in client.get("/api/v1/users/audit", headers=ADMIN).json()]
    assert "auth.identity_linked_by_invitation" in actions and "auth.login" in actions
