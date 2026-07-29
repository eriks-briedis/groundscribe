"""The lock on the front door (a slice of phase 13, pulled forward).

The system is meant to be reachable from another machine on the network, and
until now anything that could reach it could approve articles, cancel runs and
read the whole trace — including source material the author marked confidential.
This is the smallest thing that fixes that: one shared password, held outside the
repository, exchanged for a signed cookie.

What it is *not* is the rest of phase 13. There are no user accounts, no
transport security, no rate limiting, and attribution still names a fixed author
— a person who knows the password is the author, as far as this system can tell.
Those are stated here so the next reader does not mistake this for more than it
is.

The tests are mostly about what must fail. An authentication check is only ever
tested by the requests it refuses; the one it lets through proves nothing on its
own.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.api.auth import SESSION_COOKIE, SESSION_TTL_SECONDS, issue_session
from groundscribe.storage.snapshot_store import SnapshotStore
from service_helpers import Harness, build_harness

PASSWORD = "correct horse battery staple"


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    """The application as it is *served*: with a password configured."""
    return TestClient(
        create_app(runtime_factory=lambda: harness.runtime, password=PASSWORD),
        # Off by default so each test says for itself whether it is signed in.
        cookies=None,
    )


def sign_in(client: TestClient, password: str = PASSWORD) -> None:
    response = client.post("/auth/login", json={"password": password})
    assert response.status_code == 204, response.text


# ----------------------------------------------------------------------
# What is refused
# ----------------------------------------------------------------------


def test_a_request_without_a_session_is_refused(client: TestClient) -> None:
    """Every route, not a list of them: a new endpoint is protected by default."""
    assert client.get("/projects/p1").status_code == 401
    assert client.get("/projects/p1/dashboard").status_code == 401
    assert client.post("/projects", json={}).status_code == 401
    assert client.get("/executions/e1/inspect").status_code == 401


def test_the_wrong_password_gets_no_session(client: TestClient) -> None:
    response = client.post("/auth/login", json={"password": "hunter2"})

    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies
    assert client.get("/projects/p1").status_code == 401


def test_a_forged_cookie_is_refused(client: TestClient) -> None:
    """The cookie is signed, so it cannot be written by whoever holds it."""
    client.cookies.set(SESSION_COOKIE, "1780000000.not-a-real-signature")

    assert client.get("/projects/p1").status_code == 401


def test_a_session_signed_for_another_password_is_refused(client: TestClient) -> None:
    """Changing the password ends the sessions it issued.

    The signing key is derived from the password precisely so this is true:
    there is one secret to manage, and rotating it is what revocation looks like
    for a system with a single shared credential.
    """
    client.cookies.set(SESSION_COOKIE, issue_session("a different password"))

    assert client.get("/projects/p1").status_code == 401


def test_an_expired_session_is_refused(client: TestClient) -> None:
    """A cookie left in a browser is not a permanent key to the machine."""
    stale = issue_session(PASSWORD, issued_at=time.time() - SESSION_TTL_SECONDS - 1)
    client.cookies.set(SESSION_COOKIE, stale)

    assert client.get("/projects/p1").status_code == 401


# ----------------------------------------------------------------------
# What is allowed
# ----------------------------------------------------------------------


def test_signing_in_opens_the_application(client: TestClient) -> None:
    sign_in(client)

    # 404 rather than 401: past the guard, and the project genuinely is missing.
    assert client.get("/projects/p1").status_code == 404


def test_the_cookie_is_not_readable_by_scripts(client: TestClient) -> None:
    """A session a page can read is a session an injected script can steal."""
    response = client.post("/auth/login", json={"password": PASSWORD})

    (header,) = [value for key, value in response.headers.items() if key.lower() == "set-cookie"]
    assert "httponly" in header.lower()
    assert "samesite=lax" in header.lower()
    assert f"max-age={SESSION_TTL_SECONDS}" in header.lower()


def test_the_login_endpoints_are_reachable_without_a_session(client: TestClient) -> None:
    """Otherwise there would be no way in."""
    assert client.get("/auth/session").status_code == 200
    assert client.post("/auth/login", json={"password": "wrong"}).status_code == 401


def test_the_session_endpoint_says_whether_there_is_one(client: TestClient) -> None:
    """The cookie cannot be read by the app, so it has to ask."""
    assert client.get("/auth/session").json() == {"authenticated": False}

    sign_in(client)

    assert client.get("/auth/session").json() == {"authenticated": True}


def test_signing_out_ends_the_session(client: TestClient) -> None:
    sign_in(client)

    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/session").json() == {"authenticated": False}
    assert client.get("/projects/p1").status_code == 401


# ----------------------------------------------------------------------
# The unlocked application
# ----------------------------------------------------------------------


def test_without_a_password_the_application_is_open(harness: Harness) -> None:
    """No password configured, no lock — and every test in this suite relies on it.

    The choice is deliberate and its danger is handled where it belongs: the
    served application refuses to start without one, and ``scripts/dev.sh``
    writes one before it binds anything. Making the *library* refuse would mean
    every test in the repository carrying a credential to test something else.
    """
    open_client = TestClient(create_app(runtime_factory=lambda: harness.runtime))

    assert open_client.get("/projects/p1").status_code == 404
    assert open_client.get("/auth/session").json() == {"authenticated": True}
