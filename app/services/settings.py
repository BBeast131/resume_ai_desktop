"""Preferences that are remembered between runs.

Kept in an INI file under the app's data folder. App-wide values (the web app
URL) are shared; everything else is per signed-in user, so two people on one
PC keep their own choices. Nothing secret is stored here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QStandardPaths

from app.config import AppConfig, app_data_dir


def default_downloads_dir() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return Path(location) if location else Path.home() / "Downloads"


class Settings:
    def __init__(self, config: AppConfig, user_id: str | None = None) -> None:
        self.config = config
        self.user_id = user_id or ""
        path = app_data_dir() / "settings.ini"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._store = QSettings(str(path), QSettings.Format.IniFormat)

    def for_user(self, user_id: str) -> Settings:
        return Settings(self.config, user_id)

    # -- raw access ----------------------------------------------------------------

    def _key(self, name: str, per_user: bool) -> str:
        return f"users/{self.user_id}/{name}" if per_user and self.user_id else f"app/{name}"

    def get(self, name: str, default: Any = None, *, per_user: bool = True) -> Any:
        return self._store.value(self._key(name, per_user), default)

    def set(self, name: str, value: Any, *, per_user: bool = True) -> None:
        self._store.setValue(self._key(name, per_user), value)
        self._store.sync()

    def _int(self, name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _bool(self, name: str, default: bool) -> bool:
        value = self.get(name, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes")

    # -- app-wide ------------------------------------------------------------------

    @property
    def web_app_url(self) -> str:
        value = str(self.get("web_app_url", "", per_user=False) or "").strip().rstrip("/")
        return value or self.config.web_app_url

    @web_app_url.setter
    def web_app_url(self, value: str) -> None:
        self.set("web_app_url", value.strip().rstrip("/"), per_user=False)

    # -- per user ------------------------------------------------------------------

    @property
    def sidebar_collapsed(self) -> bool:
        return self._bool("sidebar_collapsed", False)

    @sidebar_collapsed.setter
    def sidebar_collapsed(self, value: bool) -> None:
        self.set("sidebar_collapsed", bool(value))

    @property
    def dashboard_period(self) -> str:
        value = str(self.get("dashboard_period", "week"))
        return value if value in ("day", "week", "month") else "week"

    @dashboard_period.setter
    def dashboard_period(self, value: str) -> None:
        self.set("dashboard_period", value)

    @property
    def start_mode(self) -> str:
        value = str(self.get("start_mode", "automation"))
        return value if value in ("automation", "human") else "automation"

    @start_mode.setter
    def start_mode(self, value: str) -> None:
        self.set("start_mode", value)

    @property
    def automation_seconds(self) -> int:
        return self._int("automation_seconds", self.config.automation_step_seconds, 1, 30)

    @automation_seconds.setter
    def automation_seconds(self, value: int) -> None:
        self.set("automation_seconds", int(value))

    @property
    def sync_interval_minutes(self) -> int:
        return self._int("sync_interval_minutes", self.config.sync_interval_minutes, 5, 720)

    @sync_interval_minutes.setter
    def sync_interval_minutes(self, value: int) -> None:
        self.set("sync_interval_minutes", int(value))

    @property
    def downloads_dir(self) -> Path:
        value = str(self.get("downloads_dir", "") or "")
        return Path(value) if value else default_downloads_dir()

    @downloads_dir.setter
    def downloads_dir(self, value: Path) -> None:
        self.set("downloads_dir", str(value))

    @property
    def candidate_name(self) -> str:
        """The name written on resumes. Empty means "use my account's full name"."""
        return str(self.get("candidate_name", "") or "").strip()

    @candidate_name.setter
    def candidate_name(self, value: str) -> None:
        self.set("candidate_name", value.strip())

    @property
    def run_log_open(self) -> bool:
        return self._bool("run_log_open", False)

    @run_log_open.setter
    def run_log_open(self, value: bool) -> None:
        self.set("run_log_open", bool(value))
