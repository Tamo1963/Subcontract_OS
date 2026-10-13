"""Local OpenID Connect test provider, for demos and tests only. NOT for production.

    uvicorn seed.mock_oidc:app --port 8098

Register it in Users & access → Sign-in providers as "Other OpenID Connect provider" with
issuer http://localhost:8098, client ID ``scos-demo`` and client secret ``demo-secret``.
Its sign-in page lets you pick a fictional person. It implements discovery, a JWKS endpoint, the
authorization-code flow with PKCE (S256) and RS256-signed ID tokens, so the OS runs the same checks
it runs against Microsoft Entra ID, Google or Okta.
"""
import base64
import hashlib
import html
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

PEOPLE = [  # fictional directory
    {"sub": "00u-anita-7f3a", "email": "anita.desai@demo-epc.example", "name": "Anita Desai", "email_verified": True},
    {"sub": "00u-vikram-91c2", "email": "vikram.rao@demo-epc.example", "name": "Vikram Rao", "email_verified": True},
    {"sub": "00u-newhire-55d1", "email": "new.engineer@demo-epc.example", "name": "New Engineer", "email_verified": True},
    {"sub": "00u-outsider-0b9e", "email": "someone@other-company.example", "name": "Outside Person", "email_verified": True},
    {"sub": "00u-unverified-c4d0", "email": "anita.desai@demo-epc.example", "name": "Unverified Claim", "email_verified": False},
]


class MockIdP:
    def __init__(self, issuer="http://localhost:8098", client_id="scos-demo", client_secret="demo-secret"):
        self.issuer, self.client_id, self.client_secret = issuer.rstrip("/"), client_id, client_secret
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "mock-" + secrets.token_hex(4)
        self.codes: dict[str, dict] = {}
        self.overrides: dict = {}     # tests tamper with tokens through this

    # -- endpoints ---------------------------------------------------------------
    def discovery(self):
        return {"issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token", "jwks_uri": f"{self.issuer}/jwks",
                "response_types_supported": ["code"], "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"], "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]}

    def jwks(self):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def authorize(self, auth_url: str, person: dict) -> tuple[str, str, str]:
        """Simulate the user signing in: returns (redirect_uri, code, state)."""
        q = {k: v[0] for k, v in parse_qs(urlparse(auth_url).query).items()}
        assert q["client_id"] == self.client_id and q["response_type"] == "code"
        assert q["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"person": person, "nonce": q["nonce"], "challenge": q["code_challenge"],
                            "redirect_uri": q["redirect_uri"], "at": time.time()}
        return q["redirect_uri"], code, q["state"]

    def token(self, form: dict, basic: str | None):
        cid, secret = form.get("client_id"), form.get("client_secret")
        if basic:
            cid, secret = base64.b64decode(basic.split()[1]).decode().split(":", 1)
        if cid != self.client_id or secret != self.client_secret:
            return 401, {"error": "invalid_client"}
        grant = self.codes.pop(form.get("code", ""), None)        # one-time codes
        if not grant or time.time() - grant["at"] > 120:
            return 400, {"error": "invalid_grant"}
        verifier = form.get("code_verifier", "")
        if base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() != grant["challenge"]:
            return 400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}
        if form.get("redirect_uri") != grant["redirect_uri"]:
            return 400, {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": self.client_id, "iat": now, "exp": now + 300, "nonce": grant["nonce"],
                  **grant["person"]}
        claims.update(self.overrides.get("claims", {}))
        key = self.overrides.get("signing_key", self.key)
        alg, headers = self.overrides.get("alg", "RS256"), {"kid": self.overrides.get("kid", self.kid)}
        id_token = jwt.encode(claims, key if alg != "HS256" else "shared", algorithm=alg, headers=headers)
        return 200, {"access_token": secrets.token_urlsafe(16), "token_type": "Bearer", "id_token": id_token}

    # -- httpx.MockTransport handler (tests) -------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.discovery())
        if path.endswith("/jwks"):
            return httpx.Response(200, json=self.jwks())
        if path.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            status, body = self.token(form, request.headers.get("authorization"))
            return httpx.Response(status, json=body)
        return httpx.Response(404)


# ---------------------------------------------------------------- runnable demo server
def _make_app():
    from fastapi import FastAPI, Form, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    idp = MockIdP()
    app = FastAPI(title="Mock OpenID Connect provider (demo only)")

    @app.get("/.well-known/openid-configuration")
    def disc():
        return idp.discovery()

    @app.get("/jwks")
    def keys():
        return idp.jwks()

    @app.get("/authorize", response_class=HTMLResponse)
    def authorize_page(request: Request):
        q = str(request.url.query)
        rows = "".join(
            f"<form method='post' action='/authorize?{html.escape(q)}'><input type='hidden' name='i' value='{i}'>"
            f"<button>{html.escape(p['name'])}<small>{html.escape(p['email'])}"
            f"{'' if p['email_verified'] else ' · e-mail NOT verified'}</small></button></form>"
            for i, p in enumerate(PEOPLE))
        return f"""<!doctype html><meta name=viewport content='width=device-width'>
        <title>Demo identity provider</title><style>body{{font-family:system-ui;background:#f3f4f6;display:grid;place-items:center;min-height:100vh;margin:0}}
        .c{{background:#fff;padding:26px 28px;border-radius:12px;width:380px;box-shadow:0 8px 30px #0002}}
        button{{display:block;width:100%;text-align:left;padding:10px 12px;margin:6px 0;border:1px solid #ddd;border-radius:8px;background:#fff;cursor:pointer;font:inherit}}
        button:hover{{background:#eef6f7}} small{{display:block;color:#666;font-size:12px}} .w{{color:#b42318;font-size:12px}}</style>
        <div class=c><h3>Demo identity provider</h3><p class=w>Test provider only — no password. Pick who you are:</p>{rows}</div>"""

    @app.post("/authorize")
    def authorize_submit(request: Request, i: int = Form(...)):
        redirect_uri, code, state = idp.authorize(str(request.url), PEOPLE[i])
        return RedirectResponse(redirect_uri + "?" + urlencode({"code": code, "state": state}), status_code=303)

    @app.post("/token")
    async def token(request: Request):
        form = {k: v for k, v in (await request.form()).items()}
        status, body = idp.token(form, request.headers.get("authorization"))
        return JSONResponse(body, status_code=status)

    return app


app = _make_app()
