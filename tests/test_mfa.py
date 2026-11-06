"""Authenticator-app codes (TOTP) for password sign-in (ADR-0007)."""
import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, config, mfa
from app.db import SessionLocal
from app.models import AuditEvent, MfaChallenge, MfaRecoveryCode, User, UserSession

CSRF = {"X-SCOS-CSRF": "1"}
PW = "Kakinada-Piping-2026"
ADMIN_PW = "Admin-Console-2026"


def set_password(username, password):
    with SessionLocal() as db:
        u = db.query(User).filter_by(username=username).one()
        u.password_hash, u.must_change_password = auth.hash_password(password), False
        db.commit()


@pytest.fixture
def c(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_DEV_AUTH", False)
    set_password("anita.contracts", PW)
    set_password("sys.admin", ADMIN_PW)
    return client


def browser(c):
    return TestClient(c.app)


def login(b, user="anita.contracts", pw=PW, **kw):
    return b.post("/api/v1/auth/login", json={"username": user, "password": pw, **kw})


def fresh_code(username="anita.contracts", secret=None):
    """The authenticator's code for the next time step not yet used (codes are single-use)."""
    with SessionLocal() as db:
        u = db.query(User).filter_by(username=username).one()
        secret = secret or mfa.decrypt(u.totp_secret_encrypted)
        step = max(mfa.current_step(), (u.totp_last_step or -1) + 1)
    return mfa.code_at(secret, step)


def wrong_code(secret):
    valid = {mfa.code_at(secret, mfa.current_step() + d) for d in (-1, 0, 1, 2)}
    return next(f"{n:06d}" for n in range(1000000) if f"{n:06d}" not in valid)


def secret_of(username="anita.contracts"):
    with SessionLocal() as db:
        return mfa.decrypt(db.query(User).filter_by(username=username).one().totp_secret_encrypted)


def enrol(b, pw=PW):
    r = b.post("/api/v1/me/mfa/totp/setup", headers=CSRF, json={"password": pw})
    assert r.status_code == 200, r.text
    secret = r.json()["secret"].replace(" ", "")
    r = b.post("/api/v1/me/mfa/totp/confirm", headers=CSRF, json={"code": mfa.code_at(secret, mfa.current_step())})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


def signed_in_with_mfa(c, user="anita.contracts", pw=PW):
    b = browser(c)
    r = login(b, user, pw)
    assert r.json()["mfa_required"] is True
    r = b.post("/api/v1/auth/login/mfa", json={"mfa_token": r.json()["mfa_token"], "code": fresh_code(user)})
    assert r.status_code == 200, r.text
    return b


def test_rfc6238_reference_vector():
    secret = base64.b32encode(b"12345678901234567890").decode()      # RFC 6238 Appendix B, T = 59 s
    assert mfa.code_at(secret, 59 // 30) == "287082"
    assert mfa.verify(secret, "287 082", None, now=59) == 1 and mfa.verify(secret, "287082", 1, now=59) is None


def test_enrolment_then_sign_in_needs_the_code(c):
    b = browser(c)
    login(b)
    assert b.get("/api/v1/me/mfa").json() == {"enabled": False, "enabled_at": None, "recovery_codes_left": 0,
                                               "required": False, "available": True, "policy": "off"}
    assert b.post("/api/v1/me/mfa/totp/setup", headers=CSRF, json={"password": "wrong"}).status_code == 401
    setup = b.post("/api/v1/me/mfa/totp/setup", headers=CSRF, json={"password": PW}).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/Subcontract%20OS%3Aanita.contracts?")
    assert setup["qr_svg"].startswith("<svg")
    secret = setup["secret"].replace(" ", "")
    assert b.post("/api/v1/me/mfa/totp/confirm", headers=CSRF, json={"code": wrong_code(secret)}).status_code == 401
    with SessionLocal() as db:                                   # stored encrypted, never in plain text
        u = db.query(User).filter_by(username="anita.contracts").one()
        assert u.totp_enabled_at is None and secret not in u.totp_pending_encrypted
    r = b.post("/api/v1/me/mfa/totp/confirm", headers=CSRF, json={"code": mfa.code_at(secret, mfa.current_step())})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10
    assert b.get("/api/v1/me/mfa").json()["recovery_codes_left"] == 10

    other = browser(c)
    r = login(other)
    assert r.status_code == 200 and r.json()["mfa_required"] is True and "bearer_token" not in r.json()
    assert other.get("/api/v1/me").status_code == 401                         # no session until the code is given
    r = other.post("/api/v1/auth/login/mfa", json={"mfa_token": r.json()["mfa_token"], "code": fresh_code()})
    assert r.status_code == 200 and r.json()["recovery_codes_left"] == 10
    me = other.get("/api/v1/me").json()
    assert me["mfa_enabled"] is True and me["signed_in_with"] == "password+totp"


def test_codes_cannot_be_replayed_and_challenges_are_single_use(c):
    b = browser(c)
    login(b)
    enrol(b)
    r = login(browser(c)).json()
    code = fresh_code()
    ok = c.post("/api/v1/auth/login/mfa", json={"mfa_token": r["mfa_token"], "code": code})
    assert ok.status_code == 200
    again = c.post("/api/v1/auth/login/mfa", json={"mfa_token": r["mfa_token"], "code": code})
    assert again.json()["error"]["code"] == "MFA_CHALLENGE_EXPIRED"                 # challenge used
    r2 = login(browser(c)).json()
    replay = c.post("/api/v1/auth/login/mfa", json={"mfa_token": r2["mfa_token"], "code": code})
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "INVALID_MFA_CODE"   # same code refused


def test_wrong_codes_count_towards_lockout_and_challenge_expires(c, monkeypatch):
    b = browser(c)
    login(b)
    enrol(b)
    monkeypatch.setattr(config, "MAX_FAILED_LOGINS", 3)
    tok = login(browser(c)).json()["mfa_token"]
    bad = wrong_code(secret_of())
    for _ in range(3):
        assert c.post("/api/v1/auth/login/mfa", json={"mfa_token": tok, "code": bad}).status_code == 401
    assert login(browser(c)).status_code == 423                                  # account locked
    with SessionLocal() as db:
        u = db.query(User).filter_by(username="anita.contracts").one()
        u.locked_until = None
        db.commit()
    tok = login(browser(c)).json()["mfa_token"]
    with SessionLocal() as db:
        db.query(MfaChallenge).update({"expires_at": auth.utcnow() - timedelta(seconds=1)})
        db.commit()
    late = c.post("/api/v1/auth/login/mfa", json={"mfa_token": tok, "code": fresh_code()})
    assert late.json()["error"]["code"] == "MFA_CHALLENGE_EXPIRED"


def test_recovery_codes_work_once_and_can_be_replaced(c):
    b = browser(c)
    login(b)
    _, codes = enrol(b)
    tok = login(browser(c)).json()["mfa_token"]
    r = c.post("/api/v1/auth/login/mfa", json={"mfa_token": tok, "code": codes[0].lower()})
    assert r.status_code == 200 and r.json()["recovery_codes_left"] == 9
    tok = login(browser(c)).json()["mfa_token"]
    assert c.post("/api/v1/auth/login/mfa", json={"mfa_token": tok, "code": codes[0]}).status_code == 401
    new = b.post("/api/v1/me/mfa/recovery-codes", headers=CSRF, json={"code": fresh_code()}).json()["recovery_codes"]
    tok = login(browser(c)).json()["mfa_token"]
    assert c.post("/api/v1/auth/login/mfa", json={"mfa_token": tok, "code": codes[1]}).status_code == 401   # old set gone
    assert c.post("/api/v1/auth/login/mfa", json={"mfa_token": tok, "code": new[0]}).status_code == 200
    with SessionLocal() as db:
        assert all(len(x.code_hash) == 64 and x.code_hash != new[0] for x in db.query(MfaRecoveryCode))
        actions = {e.action for e in db.query(AuditEvent)}
    assert {"auth.mfa_enabled", "auth.mfa_recovery_code_used", "auth.mfa_recovery_codes_replaced"} <= actions


def test_enrolling_signs_out_other_sessions(c):
    first, second = browser(c), browser(c)
    login(first)
    login(second)
    enrol(first)
    assert second.get("/api/v1/me").status_code == 401
    assert first.get("/api/v1/me").json()["signed_in_with"] == "password+totp"


def test_policy_requires_enrolment_before_other_work(c):
    admin = browser(c)
    login(admin, "sys.admin", ADMIN_PW)
    assert admin.put("/api/v1/auth/policy", headers=CSRF, json={"mfa_required": "everyone"}).status_code == 422
    r = admin.put("/api/v1/auth/policy", headers=CSRF, json={"mfa_required": "all"})
    assert r.json() == {"password_login": "all", "mfa_required": "all"}
    # the administrator is now held at enrolment as well
    assert admin.get("/api/v1/users").json()["error"]["code"] == "MFA_ENROLMENT_REQUIRED"
    assert admin.get("/api/v1/me").json()["mfa_enrolment_required"] is True
    enrol(admin, ADMIN_PW)
    assert admin.get("/api/v1/users").status_code == 200

    b = browser(c)
    login(b)
    assert b.get("/api/v1/projects").json()["error"]["code"] == "MFA_ENROLMENT_REQUIRED"
    enrol(b)
    assert b.get("/api/v1/projects").status_code == 200
    r = b.post("/api/v1/me/mfa/totp/disable", headers=CSRF, json={"password": PW, "code": fresh_code()})
    assert r.json()["error"]["code"] == "MFA_REQUIRED"


def test_admins_only_policy_and_company_sign_in_unaffected(c):
    admin = browser(c)
    login(admin, "sys.admin", ADMIN_PW)
    admin.put("/api/v1/auth/policy", headers=CSRF, json={"mfa_required": "admins"})
    b = browser(c)
    login(b)
    assert b.get("/api/v1/projects").status_code == 200                        # not an administrator
    with SessionLocal() as db:                                                 # a company (OIDC) session of the admin
        u = db.query(User).filter_by(username="sys.admin").one()
        token, th, exp = auth.new_session_token()
        db.add(UserSession(token_hash=th, user_id=u.id, expires_at=exp, method="sso"))
        db.commit()
    sso = TestClient(c.app, headers={"Authorization": f"Bearer {token}"})
    assert sso.get("/api/v1/users").status_code == 200                         # provider applies its own MFA
    assert admin.get("/api/v1/users").status_code == 403                       # password session must enrol


def test_disable_needs_password_and_code(c):
    b = browser(c)
    login(b)
    enrol(b)
    assert b.post("/api/v1/me/mfa/totp/disable", headers=CSRF,
                  json={"password": PW, "code": wrong_code(secret_of())}).status_code == 401
    assert b.post("/api/v1/me/mfa/totp/disable", headers=CSRF, json={"password": PW, "code": fresh_code()}).json() == {"enabled": False}
    r = login(browser(c))
    assert r.status_code == 200 and "mfa_required" not in r.json()


def test_admin_reset_for_a_lost_phone(c):
    b = browser(c)
    login(b)
    enrol(b)
    admin = browser(c)
    login(admin, "sys.admin", ADMIN_PW)
    users = {u["username"]: u for u in admin.get("/api/v1/users").json()["users"]}
    assert users["anita.contracts"]["mfa_enabled"] is True
    assert admin.post("/api/v1/users/sys.admin/reset-mfa", headers=CSRF).json()["error"]["code"] == "SELF_RESET"
    r = admin.post("/api/v1/users/anita.contracts/reset-mfa", headers=CSRF)
    assert r.status_code == 200 and r.json()["mfa_enabled"] is False
    assert b.get("/api/v1/me").status_code == 401                              # signed out everywhere
    assert "mfa_required" not in login(browser(c)).json()
    anita = browser(c)
    login(anita)
    assert anita.post("/api/v1/users/sys.admin/reset-mfa", headers=CSRF).status_code == 403   # administrators only


def test_bearer_token_after_code_and_passwordless_accounts(c):
    b = browser(c)
    login(b)
    enrol(b)
    tok = login(browser(c), issue_bearer_token=True).json()["mfa_token"]
    r = c.post("/api/v1/auth/login/mfa", json={"mfa_token": tok, "code": fresh_code()}).json()
    api = TestClient(c.app, headers={"Authorization": f"Bearer {r['bearer_token']}"})
    assert api.get("/api/v1/me").json()["username"] == "anita.contracts"
    with SessionLocal() as db:                                                 # company-login-only account
        u = db.query(User).filter_by(username="vikram.legal").one()
        u.password_hash = None
        token, th, exp = auth.new_session_token()
        db.add(UserSession(token_hash=th, user_id=u.id, expires_at=exp, method="sso"))
        db.commit()
    v = TestClient(c.app, headers={"Authorization": f"Bearer {token}"})
    assert v.get("/api/v1/me/mfa").json()["available"] is False
    assert v.post("/api/v1/me/mfa/totp/setup", json={"password": "x"}).json()["error"]["code"] == "NO_PASSWORD"
