"""PostgreSQL-backed persistence primitives for the CleanRoomOS runtime."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from cleanroom_os.domain import (
    CleanroomInstanceCreate,
    CleanroomInstanceRead,
    OperationalRecordCreate,
    OperationalRecordRead,
    RecordKind,
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp for persisted records."""
    return datetime.now(timezone.utc)


def normalize_utc(value: datetime) -> datetime:
    """Normalize database timestamps to aware UTC values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for the persistent runtime schema."""


class CleanroomInstanceRow(Base):
    """Database row defining one cleanroom/environment scope."""

    __tablename__ = "cleanroom_instances"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class OperationalRecordRow(Base):
    """Append-friendly instance-scoped storage for typed operational records."""

    __tablename__ = "operational_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("cleanroom_instances.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        Index("ix_operational_records_instance_kind_created", "instance_id", "kind", "created_at"),
    )


class PersistenceError(RuntimeError):
    """Base exception for persistent runtime failures exposed to API callers."""


class UnknownInstanceError(PersistenceError):
    """Raised when an operation targets an unknown cleanroom instance."""


class DuplicateIdentifierError(PersistenceError):
    """Raised when a stable identifier is already in use."""


class AuditedActionRequiredError(PersistenceError):
    """Action lifecycle records may only be written through the audited service."""


class Database:
    """Own the SQLAlchemy engine/session factory for one runtime database."""

    def __init__(self, database_url: str) -> None:
        """Store database configuration without importing/connecting a DB driver."""
        self.database_url = database_url
        self._engine: Engine | None = None
        self._sessions: sessionmaker[Session] | None = None

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
        """Enable SQLite FK enforcement for tests and local development."""
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def _configure(self) -> None:
        """Create the SQLAlchemy engine/session factory on first database use."""
        if self._engine is not None:
            return
        connect_args: dict[str, object] = {}
        if self.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        engine = create_engine(
            self.database_url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if self.database_url.startswith("sqlite"):
            event.listen(engine, "connect", self._enable_sqlite_foreign_keys)
        self._engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @property
    def engine(self) -> Engine:
        """Return the lazily configured SQLAlchemy engine."""
        self._configure()
        assert self._engine is not None
        return self._engine

    def initialize(self) -> None:
        """Deterministically create the current runtime schema if it is absent."""
        from cleanroom_os import action_runtime  # noqa: F401 - register audit tables

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Provide a transaction-scoped session that commits or rolls back atomically."""
        self._configure()
        assert self._sessions is not None
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> None:
        """Verify that the configured persistence backend is reachable."""
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    @contextmanager
    def instance_session(self, instance_id: str) -> Iterator[Session]:
        """Serialize short state/decision writes within an instance across workers."""
        with self.session() as session:
            if self.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            instance = session.scalar(
                select(CleanroomInstanceRow)
                .where(CleanroomInstanceRow.id == instance_id)
                .with_for_update()
            )
            if instance is None:
                raise UnknownInstanceError(f"Unknown cleanroom instance: {instance_id}")
            yield session

    def dispose(self) -> None:
        """Release pooled database resources."""
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._sessions = None


class RuntimeRepository:
    """Typed repository for instance-scoped CleanRoomOS operational state."""

    def __init__(self, database: Database) -> None:
        """Bind repository operations to a configured database."""
        self.database = database

    def create_instance(self, data: CleanroomInstanceCreate) -> CleanroomInstanceRead:
        """Persist a cleanroom instance with a stable ID and timestamps."""
        row = CleanroomInstanceRow(
            id=data.id or uuid4().hex,
            name=data.name,
            attributes=data.attributes,
        )
        try:
            with self.database.session() as session:
                session.add(row)
                session.flush()
        except IntegrityError as exc:
            raise DuplicateIdentifierError(f"Instance identifier already exists: {row.id}") from exc
        return self._instance_from_row(row)

    def get_instance(self, instance_id: str) -> CleanroomInstanceRead | None:
        """Return one cleanroom instance or None when it does not exist."""
        with self.database.session() as session:
            row = session.get(CleanroomInstanceRow, instance_id)
            return None if row is None else self._instance_from_row(row)

    def list_instances(self) -> list[CleanroomInstanceRead]:
        """Return all cleanroom instances ordered by creation time and ID."""
        with self.database.session() as session:
            rows = session.scalars(
                select(CleanroomInstanceRow).order_by(
                    CleanroomInstanceRow.created_at,
                    CleanroomInstanceRow.id,
                )
            ).all()
            return [self._instance_from_row(row) for row in rows]

    def create_record(
        self,
        instance_id: str,
        data: OperationalRecordCreate,
    ) -> OperationalRecordRead:
        """Persist one typed operational record inside an existing instance."""
        if data.kind in {"action", "risk_decision", "incident", "operator_intervention"}:
            raise AuditedActionRequiredError("Use the audited /actions endpoints for action lifecycle records")
        row = OperationalRecordRow(
            id=data.id or uuid4().hex,
            instance_id=instance_id,
            kind=data.kind,
            payload=data.payload.model_dump(mode="json"),
        )
        try:
            with self.database.instance_session(instance_id) as session:
                session.add(row)
                session.flush()
        except IntegrityError as exc:
            raise DuplicateIdentifierError(f"Record identifier already exists: {row.id}") from exc
        return self._record_from_row(row)

    def get_record(self, instance_id: str, record_id: str) -> OperationalRecordRead | None:
        """Return one record only when it belongs to the requested instance."""
        with self.database.session() as session:
            row = session.scalar(
                select(OperationalRecordRow).where(
                    OperationalRecordRow.instance_id == instance_id,
                    OperationalRecordRow.id == record_id,
                )
            )
            return None if row is None else self._record_from_row(row)

    def list_records(
        self,
        instance_id: str,
        kind: RecordKind | None = None,
    ) -> list[OperationalRecordRead]:
        """Return ordered records for one instance, optionally filtered by type."""
        with self.database.session() as session:
            if session.get(CleanroomInstanceRow, instance_id) is None:
                raise UnknownInstanceError(f"Unknown cleanroom instance: {instance_id}")
            statement = select(OperationalRecordRow).where(
                OperationalRecordRow.instance_id == instance_id
            )
            if kind is not None:
                statement = statement.where(OperationalRecordRow.kind == kind)
            rows = session.scalars(
                statement.order_by(OperationalRecordRow.created_at, OperationalRecordRow.id)
            ).all()
            return [self._record_from_row(row) for row in rows]

    @staticmethod
    def _instance_from_row(row: CleanroomInstanceRow) -> CleanroomInstanceRead:
        """Convert an ORM row to an isolated typed instance value."""
        return CleanroomInstanceRead(
            id=row.id,
            name=row.name,
            attributes=row.attributes,
            created_at=normalize_utc(row.created_at),
            updated_at=normalize_utc(row.updated_at),
        )

    @staticmethod
    def _record_from_row(row: OperationalRecordRow) -> OperationalRecordRead:
        """Convert an ORM row to an isolated typed operational record value."""
        return OperationalRecordRead(
            id=row.id,
            instance_id=row.instance_id,
            kind=row.kind,
            payload=row.payload,
            created_at=normalize_utc(row.created_at),
            updated_at=normalize_utc(row.updated_at),
        )
