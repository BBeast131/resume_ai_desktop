"""Column types shared by the models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    """A UUID4 as text. It is the row's identity on the server too (`client_id`)."""
    return str(uuid.uuid4())


def to_iso(value: datetime) -> str:
    """`2026-10-02T14:05:09.123Z`: UTC, millisecond precision, sorts as text."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def from_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class UtcIso(TypeDecorator[datetime]):
    """A UTC timestamp stored as ISO-8601 text.

    Text rather than SQLite's own date format so the web importer (which reads
    this file in the browser) and the sync API see exactly the same strings.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        return None if value is None else to_iso(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        return None if value is None else from_iso(str(value))
