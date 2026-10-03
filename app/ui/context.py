"""What every page needs, gathered once after sign-in."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtWebEngineCore import QWebEngineProfile

from app.config import AppConfig, user_data_dir
from app.data.store import Store
from app.services.auth import AuthService, UserProfile
from app.services.documents import DocumentService
from app.services.settings import Settings
from app.sync.api_client import ApiError, WebApi
from app.sync.engine import SyncEngine

log = logging.getLogger(__name__)

#: The web app's limits, used until the live ones arrive from GET /api/desktop/config.
DEFAULT_LIMITS: dict[str, int] = {
    "jobDescriptionMin": 100,
    "jobDescriptionMax": 50_000,
    "roleMax": 160,
    "companyMax": 160,
    "candidateNameMin": 2,
    "candidateNameMax": 120,
    "jobUrlMax": 2048,
    "chatUrlMax": 300,
    "modelOutputMax": 400_000,
    "syncBatchMax": 50,
}

_tasks: set[asyncio.Future[Any]] = set()


def spawn(coroutine: Coroutine[Any, Any, Any]) -> asyncio.Future[Any]:
    """Start a coroutine from a Qt slot. The task is kept alive and its failure is logged, not lost."""
    task = asyncio.ensure_future(coroutine)
    _tasks.add(task)

    def done(finished: asyncio.Future[Any]) -> None:
        _tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            error = finished.exception()
            # Where it failed, and the exception TYPE: a message can quote resume or page text
            # (a database error carries its statement), and that must never reach the log.
            frames = traceback.extract_tb(error.__traceback__)[-6:] if error is not None else []
            where = " < ".join(f"{frame.name}:{frame.lineno}" for frame in reversed(frames))
            log.error("task.failed %s at %s", type(error).__name__, where)

    task.add_done_callback(done)
    return task


@dataclass
class AppContext:
    config: AppConfig
    settings: Settings
    auth: AuthService
    api: WebApi
    store: Store
    sync: SyncEngine
    documents: DocumentService
    user: UserProfile
    web_profile: QWebEngineProfile | None = None
    limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    #: The text every generation prompt must end with; from the web app, cached locally.
    output_schema: str = ""
    groq_available: bool | None = None
    online: bool = False
    #: Called with "jobs", "resumes" or "status" after the local data changed.
    listeners: list[Callable[[str], None]] = field(default_factory=list)

    def changed(self, kind: str) -> None:
        """Tell the open pages (badges, dashboard) and the sync scheduler that local data changed."""
        for listener in list(self.listeners):
            listener(kind)

    @property
    def candidate_name(self) -> str:
        return self.settings.candidate_name or self.user.full_name

    def _cache_path(self) -> Any:
        return user_data_dir(self.user.id) / "server_config.json"

    def load_cached_server_config(self) -> None:
        try:
            self._adopt(json.loads(self._cache_path().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def _adopt(self, data: dict[str, Any]) -> None:
        schema = data.get("outputSchema")
        if isinstance(schema, str) and schema.strip():
            self.output_schema = schema
        limits = data.get("limits")
        if isinstance(limits, dict):
            for key, value in limits.items():
                if key in DEFAULT_LIMITS and isinstance(value, int) and value > 0:
                    self.limits[key] = value
        if "groqAvailable" in data:
            self.groq_available = bool(data.get("groqAvailable"))

    async def refresh_server_config(self) -> bool:
        """Fetch the live limits and output schema. False when the web app is unreachable."""
        try:
            data = await self.api.get_config()
        except ApiError:
            self.online = False
            return False
        self.online = True
        self._adopt(data)
        try:
            path = self._cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
        return True
