"""Sessions, API tokens, login throttling and OpenID Connect single sign-on."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, Request

from .security import ROLES, has_role, new_token, token_hash, verify_password
from .store import Store, now, parse_ts

COOKIE = "tessera_session"
CSRF_HEADER = "x-tessera-csrf"
LOCK_AFTER = 5
LOCK_MINUTES = 15


class IPThrottle:
    def __init__(self, limit: int = 30, window: int = 600):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, ip: str) -> None:
        q, t = self.hits[ip], time.time()
        while q and q[0] < t - self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(429, "Too many sign-in attempts from this address. Try again in a few minutes.")
        q.append(t)


def client_ip(req) -> str:
    """The caller's address. Behind a reverse proxy (Caddy, a load balancer, Cloudflare) every request arrives from the
    proxy, so with TESSERA_TRUST_PROXY_HEADERS=true the first X-Forwarded-For entry is used instead. Only turn that on
    when Tessera cannot be reached except through the proxy, or callers could choose their own address."""
    import os
    if os.environ.get("TESSERA_TRUST_PROXY_HEADERS", "").strip().lower() in ("1", "true", "yes", "on"):
        fwd = req.headers.get("x-forwarded-for", "")
        first = fwd.split(",")[0].strip()
        if first:
            return first[:64]
    return req.client.host if req.client else "unknown"


def create_session(store: Store, user_id: int, hours: int, req: Request) -> str:
    tok = new_token()
    store.insert("sessions", {"token_hash": token_hash(tok), "user_id": user_id, "created_at": now(),
                              "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds"),
                              "ip": client_ip(req), "user_agent": (req.headers.get("user-agent") or "")[:200]})
    store.exec("UPDATE users SET last_login=?, failed_logins=0, locked_until=NULL WHERE id=?", (now(), user_id))
    return tok


def login(store: Store, email: str, password: str) -> dict:
    u = store.one("SELECT * FROM users WHERE email=?", (email.strip(),))
    lu = parse_ts(u["locked_until"]) if u else None
    if u and lu and lu > datetime.now(timezone.utc):
        verify_password(password, None)
        raise HTTPException(423, f"Account locked after {LOCK_AFTER} failed attempts. Try again after {lu.strftime('%H:%M UTC')} or ask an admin.")
    if not u or not u["active"] or not verify_password(password, u["pw_hash"]):
        if u:
            fails = u["failed_logins"] + 1
            locked = (datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds") if fails >= LOCK_AFTER else None
            store.exec("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (0 if locked else fails, locked, u["id"]))
            store.audit(u, "auth.login_failed", u["email"], {"locked": bool(locked)})
        raise HTTPException(401, "Email or password is incorrect.")
    return u


def current_user(req: Request, store: Store) -> dict:
    authz = req.headers.get("authorization", "")
    tok = None
    if authz.lower().startswith("bearer "):
        tok = authz[7:].strip()
    elif authz.lower().startswith("basic "):
        # BI tools (Power BI, Excel, Tableau) speak Basic auth: any user name, an API token as the password.
        # Account passwords are never accepted here, so this cannot be used to guess them.
        import base64
        import binascii
        try:
            tok = base64.b64decode(authz[6:].strip()).decode("utf-8").split(":", 1)[1].strip()
        except (binascii.Error, UnicodeDecodeError, IndexError):
            raise HTTPException(401, "Use an API token as the password.") from None
    if tok is not None:
        t = store.one("SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0", (token_hash(tok),))
        if not t:
            raise HTTPException(401, "API token is invalid or revoked.")
        u = store.one("SELECT * FROM users WHERE id=? AND active=1", (t["user_id"],))
        if not u:
            raise HTTPException(401, "Token owner is deactivated.")
        store.exec("UPDATE api_tokens SET last_used=? WHERE id=?", (now(), t["id"]))
        role = t["role"] if has_role(u["role"], t["role"]) else u["role"]
        return {**u, "role": role, "via": "token"}
    tok = req.cookies.get(COOKIE)
    if not tok:
        raise HTTPException(401, "Sign in required.")
    s = store.one("SELECT * FROM sessions WHERE token_hash=?", (token_hash(tok),))
    if not s or parse_ts(s["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(401, "Your session has expired. Sign in again.")
    if req.method not in ("GET", "HEAD", "OPTIONS") and req.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "Missing CSRF header.")
    u = store.one("SELECT * FROM users WHERE id=? AND active=1", (s["user_id"],))
    if not u:
        raise HTTPException(401, "Account is deactivated.")
    return {**u, "via": "session"}


def public_user(u: dict) -> dict:
    return {k: u.get(k) for k in ("id", "email", "name", "role", "active", "created_at", "last_login", "sso_subject")} | {
        "sso": bool(u.get("sso_subject")), "locked": bool(u.get("locked_until") and parse_ts(u["locked_until"]) > datetime.now(timezone.utc))}


# ---------------------------------------------------------------- OIDC
class OIDC:
    """Authorization code flow with PKCE against any OpenID Connect provider (Entra ID, Okta, Google, Keycloak, Auth0)."""

    def __init__(self, store: Store, vault, http_factory=None):
        self.store, self.vault = store, vault
        self.http_factory = http_factory or (lambda: httpx.Client(timeout=15))
        self._disc: dict = {}

    def config(self) -> dict | None:
        c = self.store.get_setting("oidc")
        if isinstance(c, str):
            c = json.loads(c)
        return c if c and c.get("enabled") else None

    def discovery(self, issuer: str) -> dict:
        if issuer not in self._disc:
            with self.http_factory() as cl:
                r = cl.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
                r.raise_for_status()
                self._disc[issuer] = r.json()
        return self._disc[issuer]

    def login_url(self) -> str:
        c = self.config()
        if not c:
            raise HTTPException(404, "Single sign-on is not configured.")
        d = self.discovery(c["issuer"])
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        self.store.exec("DELETE FROM oidc_states WHERE created_at < ?", ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),))
        self.store.insert("oidc_states", {"state": state, "nonce": nonce, "verifier": verifier, "created_at": now()})
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"response_type": "code", "client_id": c["client_id"], "redirect_uri": c["redirect_uri"],
             "scope": c.get("scopes") or "openid email profile", "state": state, "nonce": nonce,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        return d["authorization_endpoint"] + "?" + urlencode(q)

    def callback(self, code: str, state: str) -> dict:
        c = self.config()
        if not c:
            raise HTTPException(404, "Single sign-on is not configured.")
        st = self.store.one("SELECT * FROM oidc_states WHERE state=?", (state,))
        if not st or parse_ts(st["created_at"]) < datetime.now(timezone.utc) - timedelta(minutes=10):
            raise HTTPException(400, "Sign-in request expired or is invalid. Start again.")
        self.store.exec("DELETE FROM oidc_states WHERE state=?", (state,))
        d = self.discovery(c["issuer"])
        secret = self.vault.decrypt(c.get("secret_blob")).get("client_secret")
        with self.http_factory() as cl:
            r = cl.post(d["token_endpoint"], data={"grant_type": "authorization_code", "code": code, "redirect_uri": c["redirect_uri"],
                                                    "client_id": c["client_id"], "code_verifier": st["verifier"],
                                                    **({"client_secret": secret} if secret else {})})
            if r.status_code >= 400:
                raise HTTPException(400, f"Identity provider rejected the sign-in: {r.text[:200]}")
            id_token = r.json().get("id_token")
            jwks = cl.get(d["jwks_uri"]).json()
        if not id_token:
            raise HTTPException(400, "Identity provider returned no ID token.")
        kid = jwt.get_unverified_header(id_token).get("kid")
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid or kid is None), None)
        if not key:
            raise HTTPException(400, "Signing key not found at the identity provider.")
        try:
            claims = jwt.decode(id_token, jwt.PyJWK(key).key, algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
                                audience=c["client_id"], issuer=d.get("issuer", c["issuer"]), leeway=60)
        except jwt.PyJWTError as e:
            raise HTTPException(400, f"ID token could not be verified: {e}") from None
        if claims.get("nonce") != st["nonce"]:
            raise HTTPException(400, "ID token nonce mismatch.")
        email = (claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").lower()
        if not email or "@" not in email:
            raise HTTPException(400, "Identity provider did not return an email address.")
        domains = [x.strip().lower() for x in (c.get("allowed_domains") or "").split(",") if x.strip()]
        if domains and email.split("@")[1] not in domains:
            raise HTTPException(403, f"Accounts from {email.split('@')[1]} are not allowed.")
        u = self.store.one("SELECT * FROM users WHERE email=?", (email,))
        role = c.get("default_role", "viewer")
        groups = claims.get("groups") or claims.get("roles") or []
        for r_name, grp in (c.get("role_groups") or {}).items():
            if grp and grp in groups and r_name in ROLES and has_role(r_name, role):
                role = r_name
        if not u:
            if c.get("auto_provision") is False:
                raise HTTPException(403, "No Tessera account exists for this user. Ask an admin to add you.")
            uid = self.store.insert("users", {"email": email, "name": claims.get("name") or email, "role": role,
                                              "sso_subject": claims["sub"], "created_at": now()})
            u = self.store.one("SELECT * FROM users WHERE id=?", (uid,))
            self.store.audit(u, "auth.sso_provisioned", email, {"role": role})
        elif not u["active"]:
            raise HTTPException(403, "This account is deactivated.")
        else:
            self.store.exec("UPDATE users SET sso_subject=? WHERE id=?", (claims["sub"], u["id"]))
        return u
