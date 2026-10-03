"""Alembic environment. Run programmatically by `app.data.db.Database.upgrade`."""

from __future__ import annotations

from alembic import context

from app.data.models import Base

target_metadata = Base.metadata


def run_migrations() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:  # pragma: no cover - the app always passes one
        raise RuntimeError("Run migrations through app.data.db.Database.upgrade().")
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
