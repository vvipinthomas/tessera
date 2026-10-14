from conftest import ADMIN, Client, add_user, do_setup


def test_setup_requires_code_and_strong_password(app, settings):
    with Client(app) as c:
        assert c.get("/api/setup/status").json()["needs_setup"] is True
        r = c.post("/api/setup", json={"setup_code": "WRONG", "email": "a@b.ca", "name": "A", "password": "x" * 20})
        assert r.status_code == 403
        code = (settings.data_dir / "setup-code.txt").read_text().strip()
        r = c.post("/api/setup", json={"setup_code": code, "email": "a@b.ca", "name": "A", "password": "short"})
        assert r.status_code == 400 and "12 characters" in r.json()["detail"]
        do_setup(c, settings)
        assert not (settings.data_dir / "setup-code.txt").exists()
        assert c.post("/api/setup", json={"setup_code": code}).status_code == 409
        assert c.get("/api/auth/me").json()["user"]["role"] == "admin"


def test_login_logout_and_session_cookie_flags(admin, app):
    c = Client(app)
    r = c.post("/api/auth/login", json=ADMIN)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/auth/me").status_code == 200
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401


def test_wrong_password_and_lockout(admin, app):
    c = Client(app)
    for _ in range(5):
        assert c.post("/api/auth/login", json={**ADMIN, "password": "nope nope nope"}).status_code == 401
    r = c.post("/api/auth/login", json=ADMIN)
    assert r.status_code == 423, "correct password must be refused while locked"
    users = admin.get("/api/users").json()
    me = next(u for u in users if u["email"] == ADMIN["email"])
    assert me["locked"]
    admin.patch(f"/api/users/{me['id']}", json={"unlock": True})
    assert c.post("/api/auth/login", json=ADMIN).status_code == 200
    actions = [a["action"] for a in admin.get("/api/audit").json()]
    assert "auth.login_failed" in actions


def test_unknown_user_gets_same_error(admin, app):
    r = Client(app).post("/api/auth/login", json={"email": "ghost@acme.ca", "password": "whatever whatever"})
    assert r.status_code == 401 and r.json()["detail"] == "Email or password is incorrect."


def test_csrf_header_required_for_cookie_writes(admin):
    r = admin.post("/api/users", json={"email": "x@acme.ca", "role": "viewer"}, csrf=False)
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]


def test_role_enforcement(admin, app):
    viewer = add_user(admin, app, "v@acme.ca", "viewer")
    analyst = add_user(admin, app, "an@acme.ca", "analyst")
    engineer = add_user(admin, app, "en@acme.ca", "engineer")
    assert viewer.post("/api/sql", json={"sql": "select 1"}).status_code == 403
    assert analyst.post("/api/sql", json={"sql": "select 1"}).status_code == 200
    assert analyst.post("/api/connections", json={"type": "file", "name": "x"}).status_code == 403
    assert engineer.post("/api/connections", json={"type": "file", "name": "files"}).status_code == 200
    assert engineer.get("/api/users").status_code == 403
    assert engineer.get("/api/audit").status_code == 403
    assert analyst.get("/api/connections").status_code == 403


def test_admin_cannot_lock_themselves_out(admin):
    me = admin.get("/api/auth/me").json()["user"]
    assert admin.patch(f"/api/users/{me['id']}", json={"role": "viewer"}).status_code == 400
    assert admin.patch(f"/api/users/{me['id']}", json={"active": False}).status_code == 400


def test_deactivated_user_and_sessions_revoked(admin, app):
    c = add_user(admin, app, "leaver@acme.ca", "analyst")
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["email"] == "leaver@acme.ca")
    admin.patch(f"/api/users/{uid}", json={"active": False})
    assert c.get("/api/auth/me").status_code == 401
    assert Client(app).post("/api/auth/login", json={"email": "leaver@acme.ca", "password": "another strong pass 7"}).status_code == 401


def test_password_change_revokes_other_sessions(admin, app):
    other = Client(app)
    other.post("/api/auth/login", json=ADMIN)
    r = admin.post("/api/auth/password", json={"current_password": ADMIN["password"], "new_password": "a brand new passphrase 42"})
    assert r.status_code == 200
    assert admin.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert admin.post("/api/auth/password", json={"current_password": "wrong", "new_password": "another new passphrase 43"}).status_code == 400


def test_api_tokens(admin, app):
    analyst = add_user(admin, app, "bot-owner@acme.ca", "analyst")
    assert analyst.post("/api/tokens", json={"name": "too high", "role": "admin"}).status_code == 400
    tok = analyst.post("/api/tokens", json={"name": "ci", "role": "analyst"}).json()["token"]
    assert tok.startswith("tsr_")
    bare = Client(app)
    h = {"Authorization": f"Bearer {tok}"}
    assert bare.get("/api/auth/me", headers=h).json()["user"]["email"] == "bot-owner@acme.ca"
    # bearer requests need no CSRF header
    assert bare.post("/api/sql", json={"sql": "select 42 AS x"}, headers=h, csrf=False).json()["rows"] == [[42]]
    assert bare.post("/api/tokens", json={"name": "nested"}, headers=h).status_code == 403
    tid = analyst.get("/api/tokens").json()[0]["id"]
    analyst.delete(f"/api/tokens/{tid}")
    assert bare.get("/api/auth/me", headers=h).status_code == 401
    # token hash stored, never the token
    import sqlite3
    db = sqlite3.connect(app.state.settings.meta_path)
    assert tok not in str(db.execute("select * from api_tokens").fetchall())


def test_security_headers(admin):
    r = admin.get("/app")
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert admin.get("/api/health").headers["cache-control"] == "no-store"


def test_passwords_are_hashed(admin, app):
    import sqlite3
    row = sqlite3.connect(app.state.settings.meta_path).execute("select pw_hash from users").fetchone()[0]
    assert row.startswith("scrypt$") and ADMIN["password"] not in row
