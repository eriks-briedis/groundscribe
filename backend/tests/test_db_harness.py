"""Self-tests for the database test harness (phase 01).

Spec (plan/01 Test-first specification → Harness self-tests):
- the in-memory DB fixture creates/tears down a schema and rolls back between tests;
- the engine factory produces a working session and a trivial model round-trips.

These prove the harness itself is trustworthy before any domain model relies on it.
"""

from __future__ import annotations

from sqlalchemy import inspect, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from groundscribe.db import Base, create_engine, session_factory


class Widget(Base):
    """Trivial model used only to exercise the harness."""

    __tablename__ = "widget_harness"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()


def test_engine_factory_round_trips_a_trivial_model() -> None:
    """The engine factory yields a working session that persists and reads back a row."""
    engine = create_engine()
    Base.metadata.create_all(engine)
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
    """The fixture materialises the mapped schema (the widget table exists)."""
    table_names = set(inspect(db_session.get_bind()).get_table_names())
    assert "widget_harness" in table_names


def _insert_and_count(session: Session) -> int:
    """Insert one widget after asserting the table started empty, return the new count."""
    starting = session.scalars(select(Widget)).all()
    assert starting == [], "fixture leaked rows from a previous test — isolation broken"
    session.add(Widget(name="ephemeral"))
    session.commit()
    return len(session.scalars(select(Widget)).all())


def test_isolation_first_writer(db_session: Session) -> None:
    """First test writes (and commits) a row; teardown must roll it back."""
    assert _insert_and_count(db_session) == 1


def test_isolation_second_writer(db_session: Session) -> None:
    """Second test must not see the committed row from the first — proving rollback."""
    assert _insert_and_count(db_session) == 1
