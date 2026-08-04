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


#: How long a writer waits for the lock before giving up, in milliseconds.
#:
#: Longer than pysqlite's five-second default, because the thing being waited on
#: is a whole stage committing — its trace events, model invocations and
#: artefacts land in one transaction — and a request that collided with one
#: should wait for it rather than report a locked database to a person who did
#: nothing wrong.
BUSY_TIMEOUT_MS = 15_000


#: Execution option marking a transaction that will only ever read.
#:
#: A string key rather than a subclass or a second engine factory because that is
#: what SQLAlchemy already carries down to the connection the ``begin`` handler
#: is given, and the handler is the only place that can act on it.
READ_ONLY = "groundscribe_read_only"


def read_only(engine: Engine) -> Engine:
    """The same engine and the same pool, for transactions that only read.

    Reads get a *deferred* transaction, which under write-ahead logging is the
    difference between an application that works while a stage runs and one that
    does not: a deferred reader proceeds against the last committed snapshot, and
    an immediate one waits for a write lock it will never use.

    The promise is one-way and unenforced by SQLite: a transaction that takes
    this option and then writes gets the snapshot-upgrade refusal that
    ``BEGIN IMMEDIATE`` exists to avoid. That is the right failure — loud, at the
    write, in the code that broke the promise — and it is what makes it safe to
    hand this to the projection layer, whose whole contract is that a read
    changes nothing (plan/11).
    """
    return engine.execution_options(**{READ_ONLY: True})


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
        _install_sqlite_transaction_control(engine, on_disk=not _is_in_memory_sqlite(url))

    return engine


def _install_sqlite_transaction_control(engine: Engine, *, on_disk: bool) -> None:
    """Make pysqlite behave like a normal transactional database.

    pysqlite (the stdlib sqlite3 driver) does not emit ``BEGIN`` and silently
    commits before DDL, which breaks nested-transaction / savepoint isolation —
    an outer ``ROLLBACK`` cannot undo data a ``RELEASE SAVEPOINT`` already
    committed. The documented fix is to take transaction control away from the
    driver (``isolation_level = None``) and emit ``BEGIN`` ourselves. We also
    enable ``PRAGMA foreign_keys`` so referential integrity matches Postgres.

    A database on disk additionally gets **write-ahead logging** and a busy
    timeout, because the local installation runs two processes against it: the
    API writes for the length of a command while the worker polls the queue
    (plan/09). Under the rollback journal a commit locks the whole database and a
    reader arriving during one is refused rather than delayed, which is how
    running the pair surfaces as ``database is locked``. Both settings bring
    SQLite closer to what Postgres already does, so nothing above the engine has
    to know which it is talking to.

    The in-memory database gets neither: there is no file to write ahead into,
    and the single shared connection cannot contend with itself.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if on_disk:
            # Per connection, not once per engine: the journal mode is a property
            # of the database and persists, but the timeout is a property of the
            # connection and would otherwise be the driver's default on every
            # connection after the first.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Any) -> None:
        # ``IMMEDIATE`` on disk, and this is the setting that makes two writing
        # processes work at all. A deferred transaction begins as a reader; a
        # reader that later writes must upgrade its snapshot, and if anything
        # committed in between SQLite refuses *immediately* — the one busy case
        # where the timeout is not consulted, because waiting cannot refresh a
        # stale snapshot. Every command reads before it writes (resume the run,
        # load its position, then record what happened), so under contention with
        # the worker that refusal would be the common case rather than a rare one.
        #
        # Its cost was paid by every read as well, and that was the wrong trade.
        # The assumption underneath it — "the transactions are milliseconds long"
        # — is false for the one transaction that matters: a job holds its own for
        # the length of a model call (KNOWN-ISSUES §1), and every screen in the
        # application went dead for the duration. A transaction that says it will
        # only read gets ``BEGIN`` and proceeds against the last committed
        # snapshot, which is what write-ahead logging is for.
        if not on_disk:
            conn.exec_driver_sql("BEGIN")
            return
        reading = bool(conn.get_execution_options().get(READ_ONLY, False))
        conn.exec_driver_sql("BEGIN" if reading else "BEGIN IMMEDIATE")


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured :class:`sessionmaker` bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
