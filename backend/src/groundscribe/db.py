"""Database engine factory and declarative base.

Kept deliberately provider-neutral (plan/00-overview §Tech stack): SQLite is the
local-dev store and PostgreSQL the concurrent one, so nothing here may rely on
SQLite-specific behaviour. The one SQLite-targeted tweak we *do* make goes the
other way — enabling foreign-key enforcement — so SQLite behaves like Postgres
and migration stays trivial.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_URL = "sqlite+pysqlite:///:memory:"


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in the project."""


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
