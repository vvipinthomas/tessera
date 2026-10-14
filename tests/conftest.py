import os

import pytest
from fastapi.testclient import TestClient

from tessera.app import create_app
from tessera.config import Settings

ADMIN = {"email": "admin@acme.ca", "password": "correct horse battery 9"}
CSRF = {"x-tessera-csrf": "1"}


class Client(TestClient):
    """TestClient that sends the CSRF header like the web app does."""

    def _w(self, method, url, csrf=True, **kw):
        if csrf:
            kw["headers"] = {**CSRF, **(kw.get("headers") or {})}
        return super().request(method, url, **kw)

    def post(self, url, csrf=True, **kw):
        return self._w("POST", url, csrf, **kw)

    def put(self, url, csrf=True, **kw):
        return self._w("PUT", url, csrf, **kw)

    def patch(self, url, csrf=True, **kw):
        return self._w("PATCH", url, csrf, **kw)

    def delete(self, url, csrf=True, **kw):
        return self._w("DELETE", url, csrf, **kw)


@pytest.fixture
def settings(tmp_path):
    return Settings.from_env(data_dir=tmp_path / "data", retry_base_seconds=0.01, scheduler_interval=3600)


def make_app(settings, http_factory=None):
    return create_app(settings, http_factory=http_factory, start_scheduler=False)


def do_setup(client, settings, sample=False):
    code = (settings.data_dir / "setup-code.txt").read_text().strip()
    r = client.post("/api/setup", json={"setup_code": code, "email": ADMIN["email"], "name": "Ada Admin",
                                        "password": ADMIN["password"], "load_sample": sample})
    assert r.status_code == 200, r.text
    return r


@pytest.fixture
def app(settings):
    return make_app(settings)


@pytest.fixture
def admin(app, settings):
    with Client(app) as c:
        do_setup(c, settings)
        yield c


@pytest.fixture(scope="module")
def sample_env(tmp_path_factory):
    s = Settings.from_env(data_dir=tmp_path_factory.mktemp("sample") / "data", retry_base_seconds=0.01, scheduler_interval=3600)
    a = make_app(s)
    with Client(a) as c:
        do_setup(c, s, sample=True)
        yield {"app": a, "client": c, "settings": s}


def add_user(admin_client, app, email, role, password="another strong pass 7"):
    r = admin_client.post("/api/users", json={"email": email, "role": role, "password": password, "name": email})
    assert r.status_code == 200, r.text
    c = Client(app)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


PG_URL = os.environ.get("TESSERA_TEST_PG")  # e.g. host=127.0.0.1 port=55432 user=postgres
