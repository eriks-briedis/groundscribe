"""Portable timestamp-column tests (phase 03).

Provenance is a timeline: every execution record carries timestamps, and a
comparison between two runs is meaningless if the instants they store depend on
which database wrote them. SQLite's DATETIME silently discards ``tzinfo`` and
hands back a naive value, while PostgreSQL's ``TIMESTAMPTZ`` does not — so the
same code would produce different instants on the two backends the spec requires
us to support interchangeably (plan/00 → Tech stack: avoid SQLite-specific
behaviour so migration stays trivial).

``UTCDateTime`` closes that gap: aware in, normalised-to-UTC and aware out,
naive rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, MetaData, String, Table, insert, select
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.exc import StatementError

from groundscribe.db import UTCDateTime, create_engine

_metadata = MetaData()
_stamps = Table(
    "stamp_probe",
    _metadata,
    Column("id", String, primary_key=True),
    Column("at", UTCDateTime, nullable=True),
)


def _round_trip(value: datetime | None) -> datetime | None:
    engine = create_engine()
    _metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            conn.execute(insert(_stamps).values(id="probe", at=value))
            loaded: datetime | None = conn.execute(select(_stamps.c.at)).scalar_one()
            return loaded
    finally:
        engine.dispose()


def test_utc_datetime_round_trips_aware_and_stays_aware() -> None:
    """What goes in as an aware UTC instant comes back as one, not as naive."""
    moment = datetime(2026, 7, 25, 14, 30, 15, 123456, tzinfo=UTC)
    loaded = _round_trip(moment)
    assert loaded == moment
    assert loaded is not None and loaded.tzinfo is not None
    assert loaded.utcoffset() == timedelta(0)


def test_non_utc_input_is_normalised_to_the_same_instant_in_utc() -> None:
    """Offsets are converted, not truncated: the instant is what is preserved."""
    moment = datetime(2026, 7, 25, 16, 30, tzinfo=timezone(timedelta(hours=2)))
    loaded = _round_trip(moment)
    assert loaded == moment
    assert loaded == datetime(2026, 7, 25, 14, 30, tzinfo=UTC)


def test_naive_datetimes_are_rejected_rather_than_guessed() -> None:
    """A naive timestamp has no defined instant; guessing one corrupts the timeline.

    Asserted at the type boundary because that is where the contract lives — the
    DB layer only ever re-raises it wrapped in a ``StatementError``.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        UTCDateTime().process_bind_param(datetime(2026, 7, 25, 14, 30), sqlite_dialect())


def test_naive_datetimes_are_rejected_on_the_way_into_the_database() -> None:
    """The rejection is not merely advisory: the INSERT itself fails."""
    with pytest.raises(StatementError, match="timezone-aware"):
        _round_trip(datetime(2026, 7, 25, 14, 30))


def test_null_timestamps_round_trip() -> None:
    """An unfinished execution has no completion time; NULL must survive."""
    assert _round_trip(None) is None
