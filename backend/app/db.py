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
            segment_columns = {
                column["name"] for column in inspect(self.engine).get_columns("segments")
            }
            if "render_duration_ms" not in segment_columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE segments ADD COLUMN render_duration_ms INTEGER")
                    )
            asset_columns = {column["name"] for column in inspect(self.engine).get_columns("assets")}
            asset_migrations = {
                "thumbnail_url": "TEXT",
                "thumbnail_storage_path": "TEXT",
                "thumbnail_mime_type": "VARCHAR(160)",
                "tagging_status": "VARCHAR(24) NOT NULL DEFAULT 'idle'",
                "tagging_source": "VARCHAR(24)",
                "tagging_mode": "VARCHAR(24)",
                "tagging_generation": "INTEGER NOT NULL DEFAULT 0",
                "tagging_attempt": "INTEGER NOT NULL DEFAULT 0",
                "tagging_lease_owner": "VARCHAR(120)",
                "tagging_lease_expires_at": "DATETIME",
                "tagging_requested_at": "DATETIME",
                "tagging_started_at": "DATETIME",
                "tagging_finished_at": "DATETIME",
            }
            for name, definition in asset_migrations.items():
                if name not in asset_columns:
                    with self.engine.begin() as connection:
                        connection.execute(text(f"ALTER TABLE assets ADD COLUMN {name} {definition}"))
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_assets_tagging_claim "
                        "ON assets (tagging_status, tagging_requested_at, "
                        "tagging_lease_expires_at)"
                    )
                )
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
            # The original schema stored a singleton heartbeat at id=1. Keep
            # that row and primary key intact while allowing one durable row
            # per stable worker id. Heartbeats are ephemeral, so if an
            # interrupted pre-release migration left duplicates, retain only
            # the newest row before enforcing uniqueness.
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM worker_heartbeats WHERE id NOT IN "
                        "(SELECT MAX(id) FROM worker_heartbeats GROUP BY worker_id)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "ix_worker_heartbeats_worker_id "
                        "ON worker_heartbeats (worker_id)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_heartbeat_at "
                        "ON worker_heartbeats (heartbeat_at)"
                    )
                )
        elif self.engine.dialect.name == "postgresql":
            # PostgreSQL is supported by the worker's SKIP LOCKED claim path.
            # create_all cannot add columns to an existing assets table, so
            # keep this additive migration symmetrical with the SQLite path.
            asset_columns = {
                column["name"] for column in inspect(self.engine).get_columns("assets")
            }
            asset_tagging_migrations = {
                "tagging_status": "VARCHAR(24) NOT NULL DEFAULT 'idle'",
                "tagging_source": "VARCHAR(24)",
                "tagging_mode": "VARCHAR(24)",
                "tagging_generation": "INTEGER NOT NULL DEFAULT 0",
                "tagging_attempt": "INTEGER NOT NULL DEFAULT 0",
                "tagging_lease_owner": "VARCHAR(120)",
                "tagging_lease_expires_at": "TIMESTAMP WITH TIME ZONE",
                "tagging_requested_at": "TIMESTAMP WITH TIME ZONE",
                "tagging_started_at": "TIMESTAMP WITH TIME ZONE",
                "tagging_finished_at": "TIMESTAMP WITH TIME ZONE",
            }
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE segments ADD COLUMN IF NOT EXISTS "
                        "render_duration_ms INTEGER"
                    )
                )
                for name, definition in asset_tagging_migrations.items():
                    if name not in asset_columns:
                        connection.execute(
                            text(f"ALTER TABLE assets ADD COLUMN {name} {definition}")
                        )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_assets_tagging_claim "
                        "ON assets (tagging_status, tagging_requested_at, "
                        "tagging_lease_expires_at)"
                    )
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
