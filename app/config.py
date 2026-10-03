"""Configuration: the few values the app needs before anyone signs in.

Read from the environment or a `.env` file next to the app (see
`.env.example`). Every value here is public: the Supabase URL and the
*publishable* key are the same ones the web app ships to every browser. No
server secret ever belongs in this file.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "ResumeAI"
APP_DISPLAY_NAME = "Resume AI"
APP_VERSION = "1.0.0"
#: Version of the local database layout. Written into the file (`meta` table)
#: so the web importer can refuse a file it does not understand.
SCHEMA_VERSION = 1

DEFAULT_WEB_APP_URL = "https://beastresumebuilder.vercel.app"
DEFAULT_ALLOWED_HOSTS = ("jobright.ai", "hiring.cafe", "hiringcafe.com")
#: Sign-in pages the job sites send you to. Never reachable from the address
#: bar or the home buttons; only by following the sites' own links.
LOGIN_HOSTS = (
    "accounts.google.com",
    "accounts.youtube.com",
    "appleid.apple.com",
    "idmsa.apple.com",
    "login.microsoftonline.com",
    "login.live.com",
    "github.com",
    "auth0.com",
    "clerk.com",
    "clerk.accounts.dev",
)


def project_root() -> Path:
    """The folder holding `.env`: the repo when run from source, the exe's folder when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """A file shipped inside the app (icons, page scripts, migrations)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath(*parts)


def app_data_dir() -> Path:
    """`%APPDATA%\\ResumeAI` on Windows; the platform's equivalent elsewhere."""
    override = os.environ.get("RESUME_AI_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def user_data_dir(user_id: str) -> Path:
    """One folder per account, so two people on one PC never mix data."""
    safe = "".join(ch for ch in user_id if ch.isalnum() or ch == "-") or "unknown"
    return app_data_dir() / safe


def database_path(user_id: str) -> Path:
    return user_data_dir(user_id) / "resume_ai.db"


def is_safe_web_url(url: str) -> bool:
    """https anywhere, or plain http to this PC only (a local dev server)."""
    lowered = url.strip().lower()
    return lowered.startswith("https://") or lowered.startswith(("http://localhost", "http://127.0.0.1"))


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional
        return
    for candidate in (project_root() / ".env", app_data_dir() / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


@dataclass(frozen=True)
class AppConfig:
    web_app_url: str = DEFAULT_WEB_APP_URL
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    job_search_allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    login_hosts: tuple[str, ...] = LOGIN_HOSTS
    automation_step_seconds: int = 5
    sync_interval_minutes: int = 30
    chatgpt_reply_timeout_seconds: int = 540
    page_load_timeout_seconds: int = 45
    attention_grace_seconds: int = 20
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def auth_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_publishable_key)


def load_config() -> AppConfig:
    _load_dotenv()
    env = os.environ

    web = (env.get("WEB_APP_URL") or DEFAULT_WEB_APP_URL).strip().rstrip("/")
    early_problems: list[str] = []
    if not is_safe_web_url(web):
        # The access token travels to this address: plain http is only allowed for a local server.
        early_problems.append("WEB_APP_URL must start with https:// (or be a local address). Using the default.")
        web = DEFAULT_WEB_APP_URL
    supabase_url = (env.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (env.get("SUPABASE_PUBLISHABLE_KEY") or "").strip()
    hosts = tuple(
        host.strip().lower()
        for host in (env.get("JOB_SEARCH_ALLOWED_HOSTS") or ",".join(DEFAULT_ALLOWED_HOSTS)).split(",")
        if host.strip()
    )

    problems: list[str] = list(early_problems)
    if not supabase_url:
        problems.append("SUPABASE_URL is not set.")
    if not key:
        problems.append("SUPABASE_PUBLISHABLE_KEY is not set.")
    # A service-role or secret key must never be put in a desktop app.
    if key.startswith("sb_secret_") or "service_role" in key:
        problems.append("SUPABASE_PUBLISHABLE_KEY holds a SECRET key. Use the publishable (anon) key.")
        key = ""

    return AppConfig(
        web_app_url=web,
        supabase_url=supabase_url,
        supabase_publishable_key=key,
        job_search_allowed_hosts=hosts or DEFAULT_ALLOWED_HOSTS,
        automation_step_seconds=_int("AUTOMATION_STEP_SECONDS", 5, 1, 30),
        sync_interval_minutes=_int("SYNC_INTERVAL_MINUTES", 30, 5, 720),
        chatgpt_reply_timeout_seconds=_int("CHATGPT_REPLY_TIMEOUT_SECONDS", 540, 60, 1800),
        page_load_timeout_seconds=_int("PAGE_LOAD_TIMEOUT_SECONDS", 45, 10, 180),
        attention_grace_seconds=_int("ATTENTION_GRACE_SECONDS", 20, 5, 300),
        problems=tuple(problems),
    )
