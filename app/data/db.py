"""Opening the local database and keeping its layout up to date."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import APP_VERSION, SCHEMA_VERSION, database_path, resource_path
from app.data.models import Meta, SyncState, UserAssets


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(resource_path("app", "data", "alembic")))
    return config


class Database:
    """One account's SQLite file."""

    def __init__(self, path: Path, account_id: str) -> None:
        self.path = path
        self.account_id = account_id
        path.parent.mkdir(parents=True, exist_ok=True)
        # hide_parameters: a database error must never carry resume or job-description text into a log.
        self.engine: Engine = create_engine(f"sqlite:///{path}", future=True, hide_parameters=True)

        @event.listens_for(self.engine, "connect")
        def _pragmas(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            # WAL: the sync engine can read while a generation writes.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        self._sessions = sessionmaker(self.engine, expire_on_commit=False, future=True)

    @classmethod
    def for_user(cls, user_id: str) -> Database:
        database = cls(database_path(user_id), user_id)
        database.upgrade()
        return database

    def upgrade(self) -> None:
        """Create or migrate the tables, then stamp the file with who and what it is."""
        config = _alembic_config()
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        with self.session() as session:
            for key, value in (
                ("schema_version", str(SCHEMA_VERSION)),
                ("account_id", self.account_id),
                ("app_version", APP_VERSION),
            ):
                row = session.get(Meta, key)
                if row is None:
                    session.add(Meta(key=key, value=value))
                elif key != "account_id":
                    row.value = value
            if session.get(UserAssets, 1) is None:
                session.add(UserAssets(id=1))
            if session.get(SyncState, 1) is None:
                session.add(SyncState(id=1))

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A transaction: committed when the block ends, rolled back if it raises."""
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def meta(self, key: str) -> str | None:
        with self.session() as session:
            return session.scalar(select(Meta.value).where(Meta.key == key))

    def export_copy(self, destination: Path) -> Path:
        """A consistent single-file copy, for the web page's Import DB button."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.connect() as connection:
            # Fold the write-ahead log into the main file so one file holds everything.
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copyfile(self.path, destination)
        return destination

    def close(self) -> None:
        """Fold the write-ahead log into the main file and release it."""
        try:
            with self.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 - closing must not fail
            pass
        self.engine.dispose()
