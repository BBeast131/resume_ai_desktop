"""Shared job list: which saved jobs were shared, which came from the shared list.

No change to schema_version: the web importer reads named columns only, and the
sync payloads are unchanged.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TS = sa.String(32)


def upgrade() -> None:
    with op.batch_alter_table("saved_jobs") as batch:
        # When this profile's job was added to the shared list (or, for a job that came
        # from the list, when it arrived). Empty means "not shared yet".
        batch.add_column(sa.Column("published_at", TS))
        # True when the job came from another profile through the shared list.
        batch.add_column(sa.Column("shared", sa.Boolean, nullable=False, server_default=sa.false()))
    with op.batch_alter_table("sync_state") as batch:
        # How far this profile has read the shared list (the server's sequence number).
        batch.add_column(sa.Column("shared_cursor", sa.Integer))


def downgrade() -> None:
    with op.batch_alter_table("sync_state") as batch:
        batch.drop_column("shared_cursor")
    with op.batch_alter_table("saved_jobs") as batch:
        batch.drop_column("shared")
        batch.drop_column("published_at")
