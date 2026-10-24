"""The command line: account recovery (create-user, reset-password), version and JSON logs."""
import json
import logging
import sys

import pytest
from conftest import ADMIN, Client, do_setup

from tessera import __version__, cli
from tessera.app import create_app
from tessera.config import Settings


def run(monkeypatch, *args, password=None):
    monkeypatch.setattr(sys, "argv", ["tessera", *args])
    if password is not None:
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": password)
    return cli.main()


def test_version(monkeypatch, capsys):
    run(monkeypatch, "version")
    assert capsys.readouterr().out.strip() == __version__


def test_locked_out_admin_recovers_from_the_command_line(tmp_path, monkeypatch, capsys):
    s = Settings.from_env(data_dir=tmp_path / "d", scheduler_interval=3600)
    app = create_app(s, start_scheduler=False)
    with Client(app) as c:
        do_setup(c, s)
        for _ in range(6):  # lock the account with wrong passwords
            c.post("/api/auth/login", json={"email": ADMIN["email"], "password": "wrong password 123"})
        assert c.post("/api/auth/login", json=ADMIN).status_code == 423
    monkeypatch.setenv("TESSERA_DATA_DIR", str(s.data_dir))
    with pytest.raises(SystemExit, match="characters"):
        run(monkeypatch, "reset-password", ADMIN["email"], password="short")
    with pytest.raises(SystemExit, match="No such user"):
        run(monkeypatch, "reset-password", "nobody@acme.ca", password="a brand new passphrase 8")
    run(monkeypatch, "reset-password", ADMIN["email"], password="a brand new passphrase 8")
    run(monkeypatch, "create-user", "second@acme.ca", "--role", "engineer", password="another brand new pass 9")
    with pytest.raises(SystemExit, match="already exists"):
        run(monkeypatch, "create-user", "second@acme.ca", password="another brand new pass 9")
    assert "Password reset" in capsys.readouterr().out
    app2 = create_app(Settings.from_env(data_dir=s.data_dir, scheduler_interval=3600), start_scheduler=False)
    with Client(app2) as c2:
        assert c2.post("/api/auth/login", json={"email": ADMIN["email"], "password": "a brand new passphrase 8"}).status_code == 200, "unlocked"
    with Client(app2) as c3:
        assert c3.post("/api/auth/login", json={"email": "second@acme.ca", "password": "another brand new pass 9"}).status_code == 200
        assert c3.get("/api/auth/me").json()["user"]["role"] == "engineer"


def test_json_log_lines():
    f = cli._JsonFormatter()
    rec = logging.LogRecord("tessera", logging.ERROR, __file__, 1, "boom %s", ("x",), None)
    assert json.loads(f.format(rec))["msg"] == "boom x"
    access = logging.LogRecord("tessera.access", logging.INFO, __file__, 1, '{"path": "/api/ready"}', (), None)
    assert f.format(access) == '{"path": "/api/ready"}', "access lines are already JSON"
    try:
        raise ValueError("bad")
    except ValueError:
        rec = logging.LogRecord("tessera", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    assert "ValueError" in json.loads(f.format(rec))["exc"]
