from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent

# DU allows only one active session. Never increase this.
DU_WORKERS = 1


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    du_username: str
    du_password: str
    google_sheet_id: str
    google_worksheet: str
    google_credentials: Path
    cookie_file: Path
    root_dir: Path
    debug_du: bool
    du_workers: int
    sheet_batch_size: int


def load_config() -> Config:
    load_dotenv(ROOT_DIR / ".env")

    du_username = (os.getenv("DU_USERNAME") or "").strip()
    du_password = os.getenv("DU_PASSWORD") or ""
    google_sheet_id = (os.getenv("GOOGLE_SHEET_ID") or "").strip()
    google_worksheet = (os.getenv("GOOGLE_WORKSHEET") or "Sheet1").strip()
    credentials_name = (os.getenv("GOOGLE_CREDENTIALS") or "google_credentials.json").strip()
    debug_raw = (os.getenv("DEBUG_DU") or "false").strip().lower()
    debug_du = debug_raw in ("1", "true", "yes", "on")
    try:
        sheet_batch_size = int((os.getenv("SHEET_BATCH_SIZE") or "6").strip())
    except ValueError:
        sheet_batch_size = 6
    if sheet_batch_size < 1:
        sheet_batch_size = 1
    if sheet_batch_size > 10:
        sheet_batch_size = 10

    missing: list[str] = []
    if not du_username:
        missing.append("DU_USERNAME")
    if not du_password:
        missing.append("DU_PASSWORD")
    if not google_sheet_id:
        missing.append("GOOGLE_SHEET_ID")

    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    credentials_path = Path(credentials_name)
    if not credentials_path.is_absolute():
        credentials_path = ROOT_DIR / credentials_path

    if not credentials_path.exists():
        raise ConfigError(
            f"Google credentials file not found: {credentials_path.name}"
        )

    return Config(
        du_username=du_username,
        du_password=du_password,
        google_sheet_id=google_sheet_id,
        google_worksheet=google_worksheet,
        google_credentials=credentials_path,
        cookie_file=ROOT_DIR / "du_cookies.txt",
        root_dir=ROOT_DIR,
        debug_du=debug_du,
        du_workers=DU_WORKERS,
        sheet_batch_size=sheet_batch_size,
    )
