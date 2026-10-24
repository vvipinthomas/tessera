"""Command line: tessera serve | create-user | reset-password | backup | restore | version."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys

from . import __version__
from .config import Settings
from .security import ROLES, hash_password, password_problem
from .store import Store, now


class _Offline:
    """Minimal stand-in for Platform when backing up with the server stopped."""

    def __init__(self, settings):
        self.settings = settings
        self.wh = None


class _JsonFormatter(logging.Formatter):
    def format(self, r):
        msg = r.getMessage()
        if r.name == "tessera.access":
            try:
                json.loads(msg)  # access lines are already JSON: pass them through unchanged
                return msg
            except ValueError:
                pass
        out = {"ts": self.formatTime(r), "level": r.levelname, "logger": r.name, "msg": msg}
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out)


def mcp_bridge(url: str, token: str | None, stdin=None, stdout=None, client=None) -> int:
    """Relay newline-delimited JSON-RPC between stdio and POST {url}/mcp. Logs go to stderr only."""
    import httpx
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not token:
        print("Set TESSERA_TOKEN or pass --token (create one under Settings → API tokens).", file=sys.stderr)
        return 2
    cl = client or httpx.Client(timeout=300)
    endpoint = url.rstrip("/") + "/mcp"
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            stdout.flush()
            continue
        mid = msg.get("id") if isinstance(msg, dict) else None
        try:
            r = cl.post(endpoint, content=line, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                                         "Accept": "application/json, text/event-stream"})
            if r.status_code == 202:
                continue
            if r.status_code >= 400 and not r.headers.get("content-type", "").startswith("application/json"):
                raise RuntimeError(f"HTTP {r.status_code}")
            body = r.json()
            if r.status_code >= 400 and "jsonrpc" not in body:
                raise RuntimeError(body.get("detail") or f"HTTP {r.status_code}")
            out = body
        except Exception as e:  # noqa: BLE001
            print(f"tessera mcp: {e}", file=sys.stderr)
            if mid is None:
                continue
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"Tessera server error: {e}"}}
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tessera", description="Tessera data platform")
    ap.add_argument("--data-dir", help="Data folder (default ./tessera-data or TESSERA_DATA_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the web server")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    c = sub.add_parser("create-user", help="Create a user")
    c.add_argument("email")
    c.add_argument("--name", default="")
    c.add_argument("--role", choices=ROLES, default="admin")
    r = sub.add_parser("reset-password", help="Set a new password and unlock the account")
    r.add_argument("email")
    b = sub.add_parser("backup", help="Write a consistent backup archive (stop the server or use the admin API for a live backup)")
    b.add_argument("--out", help="Archive path (default tessera-backup-<time>.tar.gz)")
    b.add_argument("--include-key", action="store_true", help="Include master.key (store the archive securely)")
    rs = sub.add_parser("restore", help="Restore a backup archive into the data folder")
    rs.add_argument("archive")
    rs.add_argument("--force", action="store_true", help="Overwrite an existing install")
    mc = sub.add_parser("mcp", help="MCP stdio bridge to a running Tessera server (for Claude Desktop and other local MCP clients)")
    mc.add_argument("--url", default=os.environ.get("TESSERA_URL", "http://127.0.0.1:8710"), help="Tessera base URL (or TESSERA_URL)")
    mc.add_argument("--token", default=os.environ.get("TESSERA_TOKEN"), help="API token (or TESSERA_TOKEN)")
    cr = sub.add_parser("cloud-run", help="Run the server on a host without a persistent disk, backing up to S3/R2 (see tessera/cloud.py)")
    cr.add_argument("--host", default="0.0.0.0")  # nosec B104 - containers listen on all interfaces
    cr.add_argument("--port", type=int)
    sub.add_parser("version")
    a = ap.parse_args(argv)
    if a.cmd == "version":
        print(__version__)
        return
    if a.cmd == "mcp":
        sys.exit(mcp_bridge(a.url, a.token))
    settings = Settings.from_env(**({"data_dir": a.data_dir} if a.data_dir else {}))
    if a.cmd in ("backup", "restore"):
        from datetime import datetime, timezone
        from pathlib import Path

        from .enterprise import backup, restore
        if a.cmd == "backup":
            if not settings.meta_path.exists():
                sys.exit(f"No Tessera install in {settings.data_dir}")
            out = Path(a.out or f"tessera-backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.tar.gz")
            m = backup(_Offline(settings), out, include_key=a.include_key)
            print(f"Backup written to {out} ({len(m['files'])} files{', includes master.key' if m['includes_key'] else ''})")
        else:
            m = restore(Path(a.archive), settings.data_dir, force=a.force)
            print(f"Restored backup from {m['created_at']} (Tessera {m['version']}) into {settings.data_dir}")
            if not m["includes_key"]:
                print("The archive has no master.key: keep the original key file in the data folder or saved connection secrets cannot be decrypted.")
        return
    if a.cmd == "cloud-run":
        from . import cloud
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        cloud.run(settings, a.host, a.port or settings.port)
        return
    if a.cmd == "serve":
        import uvicorn
        import uvicorn.config

        from .app import create_app
        if os.environ.get("TESSERA_LOG_JSON", "").lower() in ("1", "true", "yes"):
            h = logging.StreamHandler()
            h.setFormatter(_JsonFormatter())
            logging.basicConfig(level=logging.INFO, handlers=[h])
        else:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if a.host:
            settings.host = a.host
        if a.port:
            settings.port = a.port
        app = create_app(settings)
        print(f"\n  Tessera {__version__}  →  http://{settings.host}:{settings.port}\n  Data folder: {settings.data_dir}\n")
        json_logs = os.environ.get("TESSERA_LOG_JSON", "").lower() in ("1", "true", "yes")
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", proxy_headers=True, access_log=not json_logs,
                    log_config=None if json_logs else uvicorn.config.LOGGING_CONFIG)
        return
    store = Store(settings.meta_path)
    pw = getpass.getpass("New password: ")
    if problem := password_problem(pw, a.email):
        sys.exit(problem)
    if a.cmd == "create-user":
        if store.one("SELECT id FROM users WHERE email=?", (a.email.lower(),)):
            sys.exit("That user already exists. Use reset-password.")
        store.insert("users", {"email": a.email.lower(), "name": a.name or a.email, "role": a.role, "pw_hash": hash_password(pw), "created_at": now()})
        print(f"Created {a.role} {a.email}")
    else:
        if not store.one("SELECT id FROM users WHERE email=?", (a.email.lower(),)):
            sys.exit("No such user.")
        store.exec("UPDATE users SET pw_hash=?, failed_logins=0, locked_until=NULL WHERE email=?", (hash_password(pw), a.email.lower()))
        store.exec("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE email=?)", (a.email.lower(),))
        print(f"Password reset for {a.email}")


if __name__ == "__main__":
    main()
