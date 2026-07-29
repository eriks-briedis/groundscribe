"""One shared password, exchanged for a signed cookie (a slice of phase 13).

The whole mechanism, stated once:

- A person posts the password to ``/auth/login``. It is compared in constant
  time — a comparison that returns early tells an attacker how much of the
  password it got right.
- They get back a cookie holding an issue time and a signature over it. The
  signing key is **derived from the password**, so there is one secret to
  manage and changing it invalidates every session it issued. That is what
  revocation looks like for a system with a single credential.
- Every other request must carry that cookie. The check is middleware rather
  than a dependency, so a route added tomorrow is protected because it exists
  rather than because someone remembered to decorate it.

The cookie is ``HttpOnly`` (a session a page can read is a session an injected
script can steal) and ``SameSite=Lax`` (so another site cannot ride it), and it
expires. It is *not* ``Secure``: this is served over plain HTTP on a local
network, and a cookie marked ``Secure`` would simply never be sent. The password
therefore crosses the network in the clear, which is a real limitation and the
reason the rest of plan/13 exists.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

#: The cookie a browser carries. Named for the product so two local tools on the
#: same origin cannot overwrite each other's sessions.
SESSION_COOKIE = "groundscribe_session"

#: How long a session lasts. A week: long enough not to be a nuisance on a
#: machine you use daily, short enough that a browser left on an old laptop
#: stops being a key to the pipeline.
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

#: Domain separation for the signing key, so the derived key cannot be confused
#: with any other use of the same password.
_KEY_CONTEXT = b"groundscribe.session.v1"


def _signing_key(password: str) -> bytes:
    return hmac.new(_KEY_CONTEXT, password.encode("utf-8"), hashlib.sha256).digest()


def _sign(payload: str, password: str) -> str:
    digest = hmac.new(_signing_key(password), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_session(password: str, *, issued_at: float | None = None) -> str:
    """A cookie value saying when this session began, and that we said so."""
    stamp = str(int(issued_at if issued_at is not None else time.time()))
    return f"{stamp}.{_sign(stamp, password)}"


def session_is_valid(token: str | None, password: str, *, now: float | None = None) -> bool:
    """Whether ``token`` is one we issued for ``password`` and has not expired."""
    if not token or "." not in token:
        return False

    stamp, _, signature = token.partition(".")
    if not hmac.compare_digest(signature, _sign(stamp, password)):
        return False

    try:
        issued_at = int(stamp)
    except ValueError:
        return False

    moment = now if now is not None else time.time()
    # A cookie issued in the future is a clock that moved or a token that was
    # tampered with in a way the signature happened to allow; neither is a
    # session this process opened.
    return 0 <= moment - issued_at <= SESSION_TTL_SECONDS


def password_matches(offered: str, password: str) -> bool:
    """Constant-time comparison, so failure tells nothing about the password."""
    return hmac.compare_digest(offered.encode("utf-8"), password.encode("utf-8"))


class LoginRequest(BaseModel):
    """What a person types into the only form that is served unauthenticated."""

    password: str


class SessionState(BaseModel):
    """Whether this browser is signed in.

    The app cannot answer that for itself: the cookie is ``HttpOnly``, which is
    the point, so the page has to ask the side that can read it.
    """

    authenticated: bool


router = APIRouter(prefix="/auth", tags=["auth"])

#: Paths reachable without a session. Everything else is refused by the guard —
#: including anything added later, which is why these are prefixes and not a list
#: of endpoints.
#:
#: ``/health`` joins ``/auth`` in phase 14 because a container orchestrator has
#: no credential and never will: a health check that had to sign in would report
#: the stack unhealthy for as long as the password was wrong, which is precisely
#: backwards. It answers with liveness and nothing else — no counts, no ids, no
#: configuration — so an unauthenticated caller learns only that something is
#: listening, which they could tell from the socket anyway.
PUBLIC_PREFIXES = ("/auth", "/health")


@router.post("/login", status_code=204)
def login(body: LoginRequest, request: Request, response: Response) -> Response:
    """Exchange the shared password for a session cookie."""
    password = configured_password_of(request)
    if password is None:
        # No lock configured: saying "signed in" is the honest answer, and the
        # served application refuses to start in this state anyway.
        return Response(status_code=204)

    if not password_matches(body.password, password):
        raise HTTPException(status_code=401, detail="that is not the password")

    response.set_cookie(
        SESSION_COOKIE,
        issue_session(password),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return Response(status_code=204, headers=dict(response.headers))


@router.post("/logout", status_code=204)
def logout() -> Response:
    """End the session by taking the cookie back."""
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/session", response_model=SessionState)
def session(request: Request) -> SessionState:
    """Whether the caller is signed in, asked rather than assumed."""
    password = configured_password_of(request)
    if password is None:
        return SessionState(authenticated=True)
    return SessionState(
        authenticated=session_is_valid(request.cookies.get(SESSION_COOKIE), password)
    )


def configured_password_of(request: Request) -> str | None:
    """The password this application was built with, if any."""
    configured: str | None = getattr(request.app.state, "password", None)
    return configured


__all__ = [
    "PUBLIC_PREFIXES",
    "SESSION_COOKIE",
    "SESSION_TTL_SECONDS",
    "LoginRequest",
    "SessionState",
    "issue_session",
    "password_matches",
    "router",
    "session_is_valid",
]
