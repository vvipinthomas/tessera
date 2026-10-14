"""Single sign-on against a fake OpenID Connect provider with real RSA-signed ID tokens."""
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from conftest import Client, do_setup, make_app
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.example.test"


class FakeIdP:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.claims = {}
        self.nonce = None
        self.tamper = False

    def jwk(self):
        pub = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**pub, "kid": "k1", "use": "sig", "alg": "RS256"}

    def handler(self, req: httpx.Request):
        u = str(req.url)
        if u.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                                             "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks"})
        if u.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if u.endswith("/token"):
            form = parse_qs(req.content.decode())
            assert form["code_verifier"][0], "PKCE verifier must be sent"
            claims = {"iss": ISSUER, "aud": "tessera-client", "sub": "user-123", "exp": int(time.time()) + 300,
                      "iat": int(time.time()), "nonce": self.nonce, "email": "sso.user@acme.ca", "name": "Sso User", **self.claims}
            tok = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
            if self.tamper:
                tok = tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB")
            return httpx.Response(200, json={"id_token": tok, "access_token": "x"})
        return httpx.Response(404)


@pytest.fixture
def sso(settings):
    idp = FakeIdP()
    app = make_app(settings, http_factory=lambda: httpx.Client(transport=httpx.MockTransport(idp.handler)))
    c = Client(app)
    do_setup(c, settings)
    r = c.put("/api/admin/sso", json={"enabled": True, "issuer": ISSUER, "client_id": "tessera-client", "client_secret": "s3cret",
                                      "redirect_uri": "http://localhost/api/auth/oidc/callback", "default_role": "analyst",
                                      "allowed_domains": "acme.ca", "role_groups": {"engineer": "data-eng"}})
    assert r.status_code == 200
    assert "client_secret" not in json.dumps(c.get("/api/admin/sso").json()).replace("client_secret_set", "")
    return app, idp


def start(app, idp):
    browser = Client(app)
    r = browser.get("/api/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"]
    idp.nonce = q["nonce"][0]
    return browser, q["state"][0]


def test_sso_login_provisions_user(sso):
    app, idp = sso
    browser, state = start(app, idp)
    r = browser.get(f"/api/auth/oidc/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/app"
    me = browser.get("/api/auth/me").json()["user"]
    assert me["email"] == "sso.user@acme.ca" and me["role"] == "analyst" and me["sso"]
    # state is single use
    assert Client(app).get(f"/api/auth/oidc/callback?code=abc&state={state}", follow_redirects=False).status_code == 400


def test_sso_group_mapping(sso):
    app, idp = sso
    idp.claims = {"groups": ["data-eng"], "email": "eng@acme.ca"}
    browser, state = start(app, idp)
    browser.get(f"/api/auth/oidc/callback?code=abc&state={state}", follow_redirects=False)
    assert browser.get("/api/auth/me").json()["user"]["role"] == "engineer"


def test_sso_rejects_other_domains(sso):
    app, idp = sso
    idp.claims = {"email": "intruder@evil.example"}
    browser, state = start(app, idp)
    assert browser.get(f"/api/auth/oidc/callback?code=abc&state={state}", follow_redirects=False).status_code == 403


def test_sso_rejects_bad_signature_and_nonce(sso):
    app, idp = sso
    idp.tamper = True
    browser, state = start(app, idp)
    assert browser.get(f"/api/auth/oidc/callback?code=abc&state={state}", follow_redirects=False).status_code == 400
    idp.tamper = False
    browser, state = start(app, idp)
    idp.nonce = "replayed-nonce"
    assert browser.get(f"/api/auth/oidc/callback?code=abc&state={state}", follow_redirects=False).status_code == 400
