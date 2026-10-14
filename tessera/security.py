"""Password hashing, tokens, roles and secret encryption."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROLES = ("viewer", "analyst", "engineer", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
MIN_PASSWORD = 12

ROLE_HELP = {
    "viewer": "Views dashboards built on published data.",
    "analyst": "Queries published (masked) data, builds charts and dashboards, uses AI agents.",
    "engineer": "Manages connections, pipelines, quality rules and raw data.",
    "admin": "Everything, plus users, SSO, AI providers and audit log.",
}


def has_role(user_role: str, needed: str) -> bool:
    return RANK.get(user_role, -1) >= RANK[needed]


def restricted(role: str) -> bool:
    return RANK.get(role, 0) < RANK["engineer"]


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str | None) -> bool:
    if not stored:
        # constant-ish time dummy work so missing users are not distinguishable by timing
        hashlib.scrypt(pw.encode(), salt=b"0" * 16, n=2 ** 14, r=8, p=1, dklen=32)
        return False
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def password_problem(pw: str, email: str = "") -> str | None:
    if len(pw or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    local = email.split("@")[0].lower() if email else ""
    if len(local) >= 4 and local in pw.lower():
        return "The password should not contain your email name."
    if len(set(pw)) < 5:
        return "Use a less repetitive password."
    return None


def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Vault:
    """AES-256-GCM encryption for connection credentials and provider keys."""

    def __init__(self, data_dir: Path):
        env = os.environ.get("TESSERA_MASTER_KEY")
        if env:
            key = base64.b64decode(env)
        else:
            kp = data_dir / "master.key"
            if kp.exists():
                key = base64.b64decode(kp.read_text().strip())
            else:
                key = AESGCM.generate_key(bit_length=256)
                kp.write_text(base64.b64encode(key).decode())
                try:
                    os.chmod(kp, 0o600)
                except OSError:
                    pass
        if len(key) != 32:
            raise ValueError("TESSERA_MASTER_KEY must be 32 bytes, base64 encoded")
        self._aes = AESGCM(key)
        self._hmac_key = hashlib.sha256(b"tessera-mask|" + key).digest()

    def encrypt(self, obj: dict) -> str:
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, json.dumps(obj).encode(), b"tessera-v1")
        return base64.b64encode(nonce + ct).decode()

    def decrypt(self, blob: str | None) -> dict:
        if not blob:
            return {}
        raw = base64.b64decode(blob)
        return json.loads(self._aes.decrypt(raw[:12], raw[12:], b"tessera-v1"))

    @property
    def mask_salt(self) -> str:
        """Install-specific secret salt for keyed hashing of masked values."""
        return self._hmac_key.hex()
