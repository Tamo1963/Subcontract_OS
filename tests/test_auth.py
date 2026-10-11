"""Authentication: temporary passwords, forced change, lockout, sessions, CSRF, user administration."""
from datetime import timedelta

import pytest

from app import auth, config
from app.db import SessionLocal
from app.models import User, UserSession

CSRF = {"X-SCOS-CSRF": "1"}
STRONG = "Kakinada-Piping-2026"


def set_password(username, password, must_change=True):
    with SessionLocal() as db:
        u = db.query(User).filter_by(username=username).one()
        u.password_hash, u.must_change_password = auth.hash_password(password), must_change
        db.commit()


@pytest.fixture
def real_auth(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_DEV_AUTH", False)
    return client


def login(c, user, pw):
    return c.post("/api/v1/auth/login", json={"username": user, "password": pw})


def test_dev_header_rejected_when_disabled(real_auth):
    assert real_auth.get("/api/v1/me", headers={"X-User": "anita.contracts"}).status_code == 401
    assert real_auth.get("/api/v1/auth/config").json()["dev_auth"] is False


def test_temporary_password_forces_change_before_any_other_call(real_auth):
    c = real_auth
    set_password("anita.contracts", "Temp-abcd-efgh-1")
    r = login(c, "anita.contracts", "Temp-abcd-efgh-1")
    assert r.status_code == 200 and r.json()["must_change_password"] is True and "bearer_token" not in r.json()
    assert c.get("/api/v1/me").json()["must_change_password"] is True
    blocked = c.get("/api/v1/projects")
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    weak = c.post("/api/v1/auth/change-password", headers=CSRF,
                  json={"current_password": "Temp-abcd-efgh-1", "new_password": "short1A"})
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "WEAK_PASSWORD"
    ok = c.post("/api/v1/auth/change-password", headers=CSRF,
                json={"current_password": "Temp-abcd-efgh-1", "new_password": STRONG})
    assert ok.status_code == 200
    assert c.get("/api/v1/projects").status_code == 200


def test_password_stored_as_salted_hash(client):
    set_password("vikram.legal", STRONG, must_change=False)
    with SessionLocal() as db:
        h = db.query(User).filter_by(username="vikram.legal").one().password_hash
    assert STRONG not in h and h.startswith("pbkdf2_sha256$") and auth.verify_password(STRONG, h)
    assert auth.hash_password(STRONG) != auth.hash_password(STRONG)   # per-user salt


def test_generic_error_and_lockout(real_auth):
    c = real_auth
    set_password("vikram.legal", STRONG, must_change=False)
    unknown = login(c, "nobody", "x")
    wrong = login(c, "vikram.legal", "wrong-password")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]
    for _ in range(config.MAX_FAILED_LOGINS - 1):
        login(c, "vikram.legal", "wrong-password")
    locked = login(c, "vikram.legal", STRONG)             # even the right password is refused while locked
    assert locked.status_code == 423
    c.cookies.clear()
    set_password("sys.admin", STRONG, must_change=False)
    login(c, "sys.admin", STRONG)
    c.patch("/api/v1/users/vikram.legal", headers=CSRF, json={"unlock": True})
    c.cookies.clear()
    assert login(c, "vikram.legal", STRONG).status_code == 200


def test_csrf_header_required_for_cookie_writes(world, real_auth):
    c = real_auth
    set_password("ravi.registry", STRONG, must_change=False)
    login(c, "ravi.registry", STRONG)
    body = {"legal_name": "Coastal Insulation Pvt Ltd", "registration_number": "U74999MH2015PTC260001",
            "country_code": "IN"}
    assert c.post("/api/v1/subcontractors", json=body).status_code == 403
    assert c.post("/api/v1/subcontractors", json=body, headers=CSRF).status_code == 201


def test_logout_revokes_session(real_auth):
    c = real_auth
    set_password("dev.audit", STRONG, must_change=False)
    login(c, "dev.audit", STRONG)
    token = c.cookies.get(auth.COOKIE)
    assert c.get("/api/v1/me").status_code == 200
    c.post("/api/v1/auth/logout", headers=CSRF)
    c.cookies.set(auth.COOKIE, token)                      # replaying the old cookie fails
    assert c.get("/api/v1/me").status_code == 401


def test_session_expiry(real_auth):
    c = real_auth
    set_password("dev.audit", STRONG, must_change=False)
    login(c, "dev.audit", STRONG)
    with SessionLocal() as db:
        for s in db.query(UserSession).all():
            s.expires_at = auth.utcnow() - timedelta(minutes=1)
        db.commit()
    assert c.get("/api/v1/me").status_code == 401


def test_bearer_token_for_api_clients(real_auth):
    c = real_auth
    set_password("dev.audit", STRONG, must_change=False)
    tok = c.post("/api/v1/auth/login", json={"username": "dev.audit", "password": STRONG,
                                             "issue_bearer_token": True}).json()["bearer_token"]
    c.cookies.clear()
    r = c.get("/api/v1/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["username"] == "dev.audit"
    # bearer requests do not need the CSRF header (no ambient credentials)
    assert c.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.get("/api/v1/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_admin_creates_user_with_one_time_temporary_password(real_auth):
    c = real_auth
    set_password("sys.admin", STRONG, must_change=False)
    login(c, "sys.admin", STRONG)
    r = c.post("/api/v1/users", headers=CSRF, json={"username": "kiran.commercial", "full_name": "Kiran Shah",
                                                    "roles": ["commercial_manager"], "clearance": "CONFIDENTIAL"})
    assert r.status_code == 201 and r.json()["temporary_password"].startswith("Temp-")
    temp = r.json()["temporary_password"]
    listed = c.get("/api/v1/users").json()
    assert temp not in str(listed) and "password_hash" not in str(listed)
    assert c.post("/api/v1/users", headers=CSRF, json={"username": "x.y", "full_name": "Bad role",
                                                       "roles": ["superuser"]}).status_code == 422
    c.cookies.clear()
    assert login(c, "kiran.commercial", temp).json()["must_change_password"] is True


def test_reset_password_revokes_sessions_and_non_admin_denied(real_auth):
    c = real_auth
    set_password("arjun.qaqc", STRONG, must_change=False)
    login(c, "arjun.qaqc", STRONG)
    arjun_cookie = c.cookies.get(auth.COOKIE)
    assert c.get("/api/v1/users").status_code == 403
    c.cookies.clear()
    set_password("sys.admin", STRONG, must_change=False)
    login(c, "sys.admin", STRONG)
    temp = c.post("/api/v1/users/arjun.qaqc/reset-password", headers=CSRF).json()["temporary_password"]
    assert temp.startswith("Temp-")
    c.cookies.clear()
    c.cookies.set(auth.COOKIE, arjun_cookie)
    assert c.get("/api/v1/me").status_code == 401


def test_admin_cannot_lock_themselves_out(real_auth):
    c = real_auth
    set_password("sys.admin", STRONG, must_change=False)
    login(c, "sys.admin", STRONG)
    r = c.patch("/api/v1/users/sys.admin", headers=CSRF, json={"is_active": False})
    assert r.status_code == 409


def test_auth_events_are_audited(real_auth):
    c = real_auth
    set_password("sys.admin", STRONG, must_change=False)
    login(c, "sys.admin", "wrong-password")
    login(c, "sys.admin", STRONG)
    actions = [e["action"] for e in c.get("/api/v1/users/audit").json()]
    assert "auth.login" in actions and "auth.login_failed" in actions
