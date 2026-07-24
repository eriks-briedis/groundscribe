"""Self-tests for the database test harness (phase 01).

Spec (plan/01 Test-first specification → Harness self-tests):
- the in-memory DB fixture creates/tears down a schema and rolls back between tests;
- the engine factory produces a working session and a trivial model round-trips.

These prove the harness itself is trustworthy before any domain model relies on it.
"""

from __future__ import annotations

from sqlalchemy import inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from groundscribe.db import create_engine, session_factory
from groundscribe.domain.models import User


class _HarnessBase(DeclarativeBase):
    """A registry of its own, deliberately separate from the application's ``Base``.

    A throwaway test model must not land on the metadata the app migrates and
    creates: it would ship in ``create_all``, and it would leave an unclassified
    table in the editorial/execution/evaluation partition asserted by
    ``test_record_categories``.
    """


class Widget(_HarnessBase):
    """Trivial model used only to exercise the engine factory."""

    __tablename__ = "widget_harness"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()


def test_engine_factory_round_trips_a_trivial_model() -> None:
    """The engine factory yields a working session that persists and reads back a row."""
    engine = create_engine()
    _HarnessBase.metadata.create_all(engine)
    try:
        factory = session_factory(engine)
        with factory() as session:
            session.add(Widget(name="alpha"))
            session.commit()
        with factory() as session:
            widget = session.scalars(select(Widget)).one()
            assert widget.name == "alpha"
    finally:
        engine.dispose()


def test_schema_is_created_for_the_fixture(db_session: Session) -> None:
    """The fixture materialises the real mapped schema, editorial and provenance alike."""
    table_names = set(inspect(db_session.get_bind()).get_table_names())
    assert {"users", "artifact_snapshots", "stage_executions"} <= table_names


def _insert_and_count(session: Session) -> int:
    """Insert one row after asserting the table started empty, return the new count.

    Uses a real mapped entity rather than a throwaway one, so the isolation being
    proved is isolation of the schema the tests actually run against.
    """
    starting = session.scalars(select(User)).all()
    assert starting == [], "fixture leaked rows from a previous test — isolation broken"
    session.add(User(id="harness-user", name="Ephemeral", email="e@example.com"))
    session.commit()
    return len(session.scalars(select(User)).all())


def test_isolation_first_writer(db_session: Session) -> None:
    """First test writes (and commits) a row; teardown must roll it back."""
    assert _insert_and_count(db_session) == 1


def test_isolation_second_writer(db_session: Session) -> None:
    """Second test must not see the committed row from the first — proving rollback."""
    assert _insert_and_count(db_session) == 1
