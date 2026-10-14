"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8710
    secure_cookies: bool = False
    session_hours: int = 12
    base_url: str = ""
    import_dirs: list[Path] = field(default_factory=list)
    scheduler_interval: float = 20.0
    retry_base_seconds: float = 2.0
    query_timeout_seconds: float = 60.0
    max_result_rows: int = 10_000
    block_private_http: bool = False
    max_upload_mb: int = 512

    @property
    def meta_path(self) -> Path:
        return self.data_dir / "tessera.db"

    @property
    def warehouse_path(self) -> Path:
        return self.data_dir / "warehouse.duckdb"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        data_dir = Path(overrides.pop("data_dir", None) or os.environ.get("TESSERA_DATA_DIR", "./tessera-data")).resolve()
        s = cls(
            data_dir=data_dir,
            host=os.environ.get("TESSERA_HOST", "127.0.0.1"),
            port=int(os.environ.get("TESSERA_PORT", "8710")),
            secure_cookies=_bool(os.environ.get("TESSERA_SECURE_COOKIES"), False),
            session_hours=int(os.environ.get("TESSERA_SESSION_HOURS", "12")),
            base_url=os.environ.get("TESSERA_BASE_URL", "").rstrip("/"),
            import_dirs=[Path(p).resolve() for p in os.environ.get("TESSERA_IMPORT_DIRS", "").split(os.pathsep) if p],
            scheduler_interval=float(os.environ.get("TESSERA_SCHEDULER_INTERVAL", "20")),
            retry_base_seconds=float(os.environ.get("TESSERA_RETRY_BASE_SECONDS", "2")),
            query_timeout_seconds=float(os.environ.get("TESSERA_QUERY_TIMEOUT", "60")),
            max_result_rows=int(os.environ.get("TESSERA_MAX_RESULT_ROWS", "10000")),
            block_private_http=_bool(os.environ.get("TESSERA_BLOCK_PRIVATE_HTTP"), False),
            max_upload_mb=int(os.environ.get("TESSERA_MAX_UPLOAD_MB", "512")),
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        s.data_dir.mkdir(parents=True, exist_ok=True)
        s.uploads_dir.mkdir(parents=True, exist_ok=True)
        s.exports_dir.mkdir(parents=True, exist_ok=True)
        return s

    def allowed_import_roots(self) -> list[Path]:
        return [self.uploads_dir.resolve(), *self.import_dirs]
