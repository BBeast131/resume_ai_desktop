"""PDF and DOCX: rendered by the web app, cached here.

The web app's own renderer and file naming are used (`POST /api/desktop/render`),
so a document made here is identical to one downloaded from the web app. The
last rendered file per record is kept, so downloading again works offline.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.config import user_data_dir
from app.sync.api_client import ApiError, WebApi

Format = Literal["pdf", "docx"]


class DocumentError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _fingerprint(resume_json: str, company: str, role: str) -> str:
    digest = hashlib.sha256()
    for part in (resume_json, "\x00", company, "\x00", role):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()[:16]


#: A cached file is re-rendered after this long when the web app is reachable, so a template
#: change made on the web reaches the desktop within a day.
CACHE_FRESH_SECONDS = 24 * 3600


@dataclass(frozen=True)
class RenderedDocument:
    #: The cached file. Its name is short and fixed (Windows paths are limited to 260 characters).
    path: Path
    #: The name the web app gave it: `Full Name_Role_Company.pdf`.
    filename: str


class DocumentService:
    def __init__(self, api: WebApi, user_id: str) -> None:
        self.api = api
        self.cache_dir = user_data_dir(user_id) / "documents"

    def _folder(self, record_id: str) -> Path:
        safe = "".join(ch for ch in record_id if ch.isalnum() or ch == "-") or "record"
        return self.cache_dir / safe

    def _entry(self, record_id: str, fmt: Format) -> dict[str, Any] | None:
        folder = self._folder(record_id)
        try:
            entry = json.loads((folder / f"{fmt}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or not (folder / f"resume.{fmt}").is_file():
            return None
        return entry

    def cached(self, record_id: str, fmt: Format, fingerprint: str | None = None) -> RenderedDocument | None:
        """The last rendered file for this record, if there is one (and, when asked, if it is current)."""
        entry = self._entry(record_id, fmt)
        if entry is None:
            return None
        if fingerprint is not None and entry.get("fingerprint") != fingerprint:
            return None
        return RenderedDocument(
            self._folder(record_id) / f"resume.{fmt}", str(entry.get("filename") or f"Resume.{fmt}")
        )

    async def get(self, *, record_id: str, resume_json: str, company: str, role: str, fmt: Format) -> RenderedDocument:
        """The document for a record: from the cache while it is fresh, otherwise rendered again.

        When the web app cannot be reached, the last rendered file is used even
        if it is old or the record has changed since.
        """
        fingerprint = _fingerprint(resume_json, company, role)
        current = self.cached(record_id, fmt, fingerprint)
        entry = self._entry(record_id, fmt) or {}
        fresh = time.time() - float(entry.get("rendered_at") or 0) < CACHE_FRESH_SECONDS
        if current is not None and fresh:
            return current

        try:
            resume: Any = json.loads(resume_json)
        except ValueError as error:
            raise DocumentError("This record's resume JSON is damaged and cannot be rendered.") from error

        try:
            content, filename = await self.api.render(resume, company or None, role or None, fmt)
        except ApiError as error:
            stale = self.cached(record_id, fmt)
            if stale is not None:
                return stale
            raise DocumentError(error.message) from error

        folder = self._folder(record_id)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"resume.{fmt}"
            path.write_bytes(content)
            (folder / f"{fmt}.json").write_text(
                json.dumps({"filename": filename, "fingerprint": fingerprint, "rendered_at": time.time()}),
                encoding="utf-8",
            )
        except OSError as error:
            raise DocumentError("The document could not be stored in the app's data folder.") from error
        return RenderedDocument(path, filename)


def save_copy(document: RenderedDocument, folder: Path) -> Path:
    """Copy a rendered file into the Downloads folder under its real name, never overwriting another file."""
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(document.filename)
    target = folder / name.name
    content = document.path.read_bytes()
    counter = 1
    while target.exists():
        if target.read_bytes() == content:
            return target  # the very same file is already there
        counter += 1
        target = folder / f"{name.stem} ({counter}){name.suffix}"
    shutil.copyfile(document.path, target)
    return target
