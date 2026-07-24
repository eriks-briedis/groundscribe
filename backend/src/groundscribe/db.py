"""Database engine factory and declarative base.

Kept deliberately provider-neutral (plan/00-overview §Tech stack): SQLite is the
local-dev store and PostgreSQL the concurrent one, so nothing here may rely on
SQLite-specific behaviour. The one SQLite-targeted tweak we *do* make goes the
other way — enabling foreign-key enforcement — so SQLite behaves like Postgres
and migration stays trivial.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect, Engine, Enum, event
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

DEFAULT_URL = "sqlite+pysqlite:///:memory:"


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in the project."""


def enum_column(enum_cls: type[Any]) -> Enum:
    """A portable, value-stored enum column type.

    ``native_enum=False`` renders a ``VARCHAR`` rather than a PostgreSQL native
    enum type, so adding a member never needs a type migration;
    ``values_callable`` stores the StrEnum *value* instead of its member name, so
    the database holds the same stable string the code and any provenance dump
    use.

    Shared by the editorial and provenance models: two copies of this three-line
    decision could drift apart and silently change how enums are stored in half
    the schema.
    """
    return Enum(enum_cls, native_enum=False, values_callable=lambda e: [m.value for m in e])


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp column that stores and returns aware UTC instants everywhere.

    SQLite's DATETIME drops ``tzinfo`` on the way in and returns a naive value on
    the way out; PostgreSQL's TIMESTAMPTZ does neither. Provenance is a timeline
    compared across runs, so that divergence would make the same code record
    different instants on the two backends the spec treats as interchangeable.

    Naive input is rejected rather than assumed to be UTC: a caller that lost the
    zone has lost the instant, and quietly inventing one corrupts the record we
    exist to be trusted on.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("provenance timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        # SQLite hands back a naive value; it is UTC by construction of the bind
        # side, so re-attaching the zone restores the instant losslessly.
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_in_memory_sqlite(url: str) -> bool:
    return _is_sqlite(url) and (":memory:" in url or url.endswith("//"))


def create_engine(url: str = DEFAULT_URL, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine with sensible cross-provider defaults.

    In-memory SQLite gets a :class:`StaticPool` so every connection shares the
    same database (otherwise each connection would get its own empty one). For
    all SQLite engines we turn on ``PRAGMA foreign_keys`` so referential
    integrity is enforced exactly as it is on Postgres.
    """
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if _is_sqlite(url):
        kwargs["connect_args"] = {"check_same_thread": False}
        if _is_in_memory_sqlite(url):
            kwargs["poolclass"] = StaticPool

    engine = sa_create_engine(url, **kwargs)

    if _is_sqlite(url):
        _install_sqlite_transaction_control(engine)

    return engine


def _install_sqlite_transaction_control(engine: Engine) -> None:
    """Make pysqlite behave like a normal transactional database.

    pysqlite (the stdlib sqlite3 driver) does not emit ``BEGIN`` and silently
    commits before DDL, which breaks nested-transaction / savepoint isolation —
    an outer ``ROLLBACK`` cannot undo data a ``RELEASE SAVEPOINT`` already
    committed. The documented fix is to take transaction control away from the
    driver (``isolation_level = None``) and emit ``BEGIN`` ourselves. We also
    enable ``PRAGMA foreign_keys`` so referential integrity matches Postgres.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured :class:`sessionmaker` bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
