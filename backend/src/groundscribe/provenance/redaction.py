"""Redaction applied before anything is persisted (phase 03).

Product principle (plan/00): *secrets and confidential material are removed
before any trace, prompt, artefact or log is written — never after*. Scrubbing
after the fact is not equivalent: the secret has already been committed to disk,
to a backup, and to whatever replicated it.

The redactor is deliberately conservative in the other direction too. Provenance
is the product; a redactor that deleted whole payloads would keep secrets out of
storage and take the audit trail with them. So every rule here replaces the
*narrowest span it can identify* with a labelled placeholder, leaving structure,
keys, and surrounding prose intact.

Three complementary rules, because no single one is sufficient:

1. **Shape** — text that looks like a credential (API key, bearer token, AWS key,
   PEM block, ``key=value`` assignment).
2. **Name** — any mapping value whose *key* names a credential, whatever the
   value looks like. Catches the plain-looking passphrase that no pattern knows.
3. **Registration** — literal values the runtime knows are secret. Catches what
   neither shape nor name can: an internal hostname, a customer name, a
   passphrase that reads like prose.

Plus author-marked confidential spans, for source material that is sensitive
without being a credential.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: Placeholder left in place of removed material. The label says *why* the span
#: was removed, which keeps a redacted record diagnosable.
PLACEHOLDER = "[REDACTED:{label}]"

#: Author-marked confidential span. Anything between the markers is dropped.
CONFIDENTIAL_OPEN = "[[CONFIDENTIAL]]"
CONFIDENTIAL_CLOSE = "[[/CONFIDENTIAL]]"

#: Fragment matching key names that denote a credential. Used both for text
#: assignments (``api_key=...``) and for mapping keys (``{"api_key": ...}``);
#: keeping one definition means the two rules cannot drift apart.
_SECRET_KEY_FRAGMENT = (
    r"password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|token|credential|authorization|private[_-]?key"
)

_SENSITIVE_KEY_RE = re.compile(_SECRET_KEY_FRAGMENT, re.IGNORECASE)

# Value characters for an assignment match. Brackets are excluded so a
# placeholder can never itself be matched — that is what makes redaction
# idempotent (see the idempotence test).
_ASSIGNMENT_VALUE = r"[^\s\"',;}\]\[]+"

_ASSIGNMENT_RE = re.compile(
    r"(?P<key>[\w.-]*(?:" + _SECRET_KEY_FRAGMENT + r"))"
    # The separator absorbs a closing key quote so JSON-shaped text
    # (``"api_key": "..."``) matches as readily as a bare ``api_key=...``.
    r"(?P<sep>[\"']?\s*[=:]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>" + _ASSIGNMENT_VALUE + r")"
    r"(?P=quote)",
    re.IGNORECASE,
)

#: Shape-based rules, applied in order. PEM blocks come first so their inner
#: lines are consumed as one span rather than picked at by narrower rules.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("bearer_token", re.compile(r"(?<=Bearer )[A-Za-z0-9._~+/-]{8,}=*")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9._-]{12,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
)

_CONFIDENTIAL_RE = re.compile(
    re.escape(CONFIDENTIAL_OPEN) + r".*?" + re.escape(CONFIDENTIAL_CLOSE),
    re.DOTALL,
)


def _placeholder(label: str) -> str:
    return PLACEHOLDER.format(label=label)


def _label_for_key(key: str) -> str:
    """A safe, readable label derived from a mapping key."""
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") or "secret"


class Redactor:
    """Removes secrets from a payload on its way to storage.

    Stateless and cheap to construct; the recorder holds one and routes every
    persisted payload through it.
    """

    def __init__(self, *, secrets: Iterable[str] = ()) -> None:
        # Longest first: redacting a longer secret before a shorter one that is a
        # substring of it avoids leaving a fragment of the longer value behind.
        self._secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def redact_text(self, text: str) -> str:
        """Return ``text`` with every recognised secret replaced by a placeholder."""
        # Registered literals first: they are known-certain, and removing them up
        # front means a pattern cannot half-match one and leave a remnant.
        for secret in self._secrets:
            text = text.replace(secret, _placeholder("secret"))

        text = _CONFIDENTIAL_RE.sub(_placeholder("confidential"), text)

        for label, pattern in _PATTERNS:
            text = pattern.sub(_placeholder(label), text)

        return _ASSIGNMENT_RE.sub(_mask_assignment, text)

    def redact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a redacted copy of a structured payload.

        A *copy*: the caller may still be holding the live request it is about to
        send, and redaction is a persistence-boundary concern, not an in-flight
        mutation.
        """
        return {key: self._redact_member(key, value) for key, value in payload.items()}

    def _redact_member(self, key: str, value: Any) -> Any:
        """Redact one mapping entry, by key name first and by value shape second."""
        if _SENSITIVE_KEY_RE.search(key):
            return _placeholder(_label_for_key(key))
        return self._redact_value(value)

    def _redact_value(self, value: Any) -> Any:
        """Recurse through JSON-shaped data, redacting the strings it reaches."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return self.redact_payload(value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        # Numbers, booleans and null carry no secrets and stay typed.
        return value


def _mask_assignment(match: re.Match[str]) -> str:
    """Replace the value of a ``key=value`` credential, keeping the key visible.

    The key is retained because knowing *that* an API key was supplied is
    provenance; knowing its value is a leak.
    """
    quote = match.group("quote")
    return f"{match.group('key')}{match.group('sep')}{quote}{_placeholder('credential')}{quote}"
