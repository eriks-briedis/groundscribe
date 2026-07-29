"""Which database the migrations actually run against (phase 14).

KNOWN-ISSUES §2, closed here because it stopped being a papercut and became a
blocker: the compose stack points the application at a database with
``GROUNDSCRIBE_DATABASE_URL``, Alembic read ``sqlalchemy.url`` from
``alembic.ini``, and the two were different files. Nothing errors. The migrations
succeed, the API starts, and the first command fails with ``no such table:
projects`` — a message that names neither cause.

The rule the tests pin is one sentence: **the migrations run against whatever
database the application will open**. Anything else is a system that can be
"successfully migrated" and unusable at the same time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from groundscribe.app.bootstrap import DATABASE_URL_ENV
from groundscribe.db import DEFAULT_URL

#: Loaded by path rather than imported: ``backend/alembic/env.py`` is a script
#: Alembic executes, not a module on the path, and it runs migrations on import.
#: Only the resolver is wanted, so it is read out of the file's namespace.
ENV_PY = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


def resolver() -> Any:
    """``_resolve_url`` from the migration environment, without running it.

    ``env.py`` calls ``run_migrations_online()`` at the bottom, so it cannot
    simply be imported. Alembic's ``context`` is absent outside a migration run,
    which is what makes the guard below fire — and that is deliberate: the
    function under test must not need a live Alembic context to answer, because
    a deployment's mistake is made before any of that exists.
    """
    namespace: dict[str, Any] = {}
    source = ENV_PY.read_text(encoding="utf-8")
    # Everything up to the first executable use of ``context``: the imports, the
    # metadata wiring and the resolver.
    head = source.split("def _configure(")[0]
    head = head.replace("config = context.config", "config = None")
    head = head.replace(
        "if config.config_file_name is not None:\n    fileConfig(config.config_file_name)", ""
    )
    exec(compile(head, str(ENV_PY), "exec"), namespace)  # noqa: S102
    return namespace["_resolve_url"]


def test_the_environment_wins_over_the_ini(monkeypatch: pytest.MonkeyPatch) -> None:
    """plan/14 → the install path works, which requires this.

    The application reads ``GROUNDSCRIBE_DATABASE_URL``
    (``app/bootstrap.py``), so the migrations must too. Deployment-time
    configuration beating a file checked into the repository is the ordering
    every other setting in this system already uses.
    """
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql+psycopg://somewhere/else")

    assert resolver()() == "postgresql+psycopg://somewhere/else"


def test_the_ini_is_used_when_the_environment_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout with no environment set still migrates its local file.

    The fallback is what makes ``alembic upgrade head`` work in a fresh clone,
    and removing it would trade one confusing failure for another.
    """
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    assert resolver()() != ""


def test_an_empty_variable_is_not_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GROUNDSCRIBE_DATABASE_URL=`` is a shell exporting nothing, which is far
    more often a mistake than a request to migrate the empty string."""
    monkeypatch.setenv(DATABASE_URL_ENV, "   ")

    assert resolver()() != "   "


def test_the_default_is_the_one_the_application_would_have_opened() -> None:
    """The last resort has to agree with `bootstrap`'s, or a clone with neither
    an ini nor an environment migrates one file and reads another — which is the
    original bug wearing a different hat."""
    from groundscribe.app.bootstrap import DEFAULT_DATABASE_URL

    assert DEFAULT_DATABASE_URL.startswith("sqlite")
    assert DEFAULT_URL.startswith("sqlite")


def test_the_migration_environment_is_still_a_runnable_script() -> None:
    """The resolver is read out of ``env.py`` by slicing its source, which would
    silently start testing nothing if the file were restructured. This is the
    canary: the slice must still contain the function, and the whole file must
    still parse."""
    source = ENV_PY.read_text(encoding="utf-8")

    assert "def _resolve_url()" in source.split("def _configure(")[0]
    compile(source, str(ENV_PY), "exec")
