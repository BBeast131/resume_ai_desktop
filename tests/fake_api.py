"""A stand-in for the web app, for UI and pipeline tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.data.store import Store
from app.services.auth import AuthService, MemoryVault, UserProfile
from app.services.documents import DocumentService
from app.services.settings import Settings
from app.sync.api_client import ApiError, OfflineError
from app.sync.engine import SyncEngine
from app.ui.context import AppContext
from tests.factories import ACCOUNT, RESUME, make_store
from tests.fake_server import FakeServer

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\nxref\n0 4\n0000000000 65535 f \n"
    b"0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n164\n%%EOF\n"
)

SCHEMA_TEXT = '{"contact": {"fullName": "string"}, "summary": ["string"]}'


class FakeApi(FakeServer):
    def __init__(self) -> None:
        super().__init__()
        self.render_calls: list[dict[str, Any]] = []
        self.validate_calls: list[str] = []
        self.check_calls: list[dict[str, Any]] = []
        #: Replies to validate-resume, in order; the last one repeats.
        self.validate_replies: list[dict[str, Any]] = [{"ok": True, "resume": RESUME, "repaired": False, "issues": []}]
        self.check_reply: dict[str, Any] | None = None
        self.admin_view: dict[str, Any] = {}
        self.users: list[dict[str, Any]] = []
        self.account_role = "user"
        self.base_url = "https://web.example"

    def with_base_url(self, base_url: str) -> FakeApi:
        return self

    async def get_account(self) -> dict[str, Any]:
        self._check()
        return {
            "profile": {"id": ACCOUNT, "fullName": "Anthony Fox", "email": "a@example.com", "role": self.account_role}
        }

    async def get_config(self) -> dict[str, Any]:
        self._check()
        return {"outputSchema": SCHEMA_TEXT, "limits": {"jobDescriptionMin": 100}, "groqAvailable": True}

    async def validate_resume(self, model_output: str) -> dict[str, Any]:
        self._check()
        self.validate_calls.append(model_output)
        index = min(len(self.validate_calls) - 1, len(self.validate_replies) - 1)
        return self.validate_replies[index]

    async def render(self, resume: Any, company: str | None, role: str | None, fmt: str) -> tuple[bytes, str]:
        self._check()
        self.render_calls.append({"company": company, "role": role, "format": fmt})
        name = f"{resume['contact']['fullName']}_{role}_{company}.{fmt}"
        return (MINIMAL_PDF if fmt == "pdf" else b"PK\x03\x04docx"), name

    async def extract_validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check()
        self.check_calls.append(payload)
        if self.check_reply is not None:
            return self.check_reply
        return {
            "role": payload["role"],
            "company": payload["company"],
            "isJobPosting": True,
            "hasJobDescription": len(payload["jdText"]) >= 100,
            "isLinkedin": False,
            "confidence": 0.9,
            "notes": "",
            "source": "groq",
            "roleCorrected": False,
            "companyCorrected": False,
        }

    async def admin_users(self, search: str = "", page: int = 1, page_size: int = 100) -> dict[str, Any]:
        self._check()
        if self.account_role != "admin":
            raise ApiError("Administrator access is required for this operation.", "FORBIDDEN", 403)
        term = search.lower()
        items = [u for u in self.users if term in u["fullName"].lower() or term in u["email"].lower()]
        return {
            "items": items,
            "page": 1,
            "pageSize": page_size,
            "total": len(items),
            "totalPages": 1,
            "hasNext": False,
        }

    async def admin_user_desktop(self, user_id: str) -> dict[str, Any]:
        self._check()
        if self.account_role != "admin":
            raise ApiError("Administrator access is required for this operation.", "FORBIDDEN", 403)
        return self.admin_view

    async def admin_user_resume(self, user_id: str, resume_id: str) -> dict[str, Any]:
        self._check()
        for row in self.admin_view.get("generatedResumes", []):
            if row["id"] == resume_id:
                return {**row, "jdText": "Remote job description " * 10, "resumeJson": RESUME}
        raise ApiError("That resume could not be found.", "NOT_FOUND", 404)


def make_context(
    tmp_path: Path, *, role: str = "user", api: FakeApi | None = None, store: Store | None = None
) -> AppContext:
    config = AppConfig(
        web_app_url="https://web.example", supabase_url="https://p.supabase.co", supabase_publishable_key="k"
    )
    user = UserProfile(id=ACCOUNT, email="john@example.com", full_name="John Doe", role=role)
    fake = api or FakeApi()
    fake.account_role = role
    store = store or make_store(tmp_path)
    auth = AuthService(config, vault=MemoryVault())
    ctx = AppContext(
        config=config,
        settings=Settings(config, user.id),
        auth=auth,
        api=fake,  # type: ignore[arg-type]
        store=store,
        sync=SyncEngine(store, fake),  # type: ignore[arg-type]
        documents=DocumentService(fake, user.id),  # type: ignore[arg-type]
        user=user,
    )
    ctx.output_schema = SCHEMA_TEXT
    ctx.settings.downloads_dir = tmp_path / "Downloads"
    return ctx


__all__ = ["FakeApi", "OfflineError", "make_context", "json"]
