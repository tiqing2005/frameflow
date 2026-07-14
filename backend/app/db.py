from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Base


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.ensure_directories()
        connect_args = {"check_same_thread": False, "timeout": 10} if settings.database_url.startswith("sqlite") else {}
        self.engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            pool_pre_ping=True,
            future=True,
        )
        if settings.database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        # create_all does not evolve an existing demo SQLite database. Keep the
        # one required fencing column backward compatible without a migration
        # service or external dependency.
        if self.settings.database_url.startswith("sqlite"):
            columns = {column["name"] for column in inspect(self.engine).get_columns("jobs")}
            if "execution_generation" not in columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE jobs ADD COLUMN execution_generation "
                            "INTEGER NOT NULL DEFAULT 0"
                        )
                    )
            if "kind" not in columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE jobs ADD COLUMN kind "
                            "VARCHAR(24) NOT NULL DEFAULT 'pipeline'"
                        )
                    )
                    connection.execute(
                        text("CREATE INDEX IF NOT EXISTS ix_jobs_kind ON jobs (kind)")
                    )
            asset_columns = {column["name"] for column in inspect(self.engine).get_columns("assets")}
            asset_migrations = {
                "thumbnail_url": "TEXT",
                "thumbnail_storage_path": "TEXT",
                "thumbnail_mime_type": "VARCHAR(160)",
            }
            for name, definition in asset_migrations.items():
                if name not in asset_columns:
                    with self.engine.begin() as connection:
                        connection.execute(text(f"ALTER TABLE assets ADD COLUMN {name} {definition}"))
            heartbeat_columns = {
                column["name"] for column in inspect(self.engine).get_columns("worker_heartbeats")
            }
            if "operational_state" not in heartbeat_columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE worker_heartbeats ADD COLUMN operational_state "
                            "VARCHAR(24) NOT NULL DEFAULT 'ready'"
                        )
                    )
            if "status_detail" not in heartbeat_columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE worker_heartbeats ADD COLUMN status_detail TEXT")
                    )
        from .seed import seed_assets

        with self.session() as session:
            seed_assets(session, self.settings)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
