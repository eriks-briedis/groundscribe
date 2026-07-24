"""Redaction-hook tests (phase 03).

Spec (plan/00 → *Redaction before persistence*; plan/03 → *Security and privacy →
Secret management*): secrets and confidential material are removed **before** any
trace, prompt, artefact, or record is written — never scrubbed afterwards. This
module tests the redactor in isolation; ``test_provenance_recorder`` proves it is
actually wired into the single persistence path.

The invariant under test is two-sided: the secret must be gone, *and* the record
must survive. Redaction that drops the whole payload would satisfy the first half
and destroy the provenance the product exists to provide.
"""

from __future__ import annotations

from typing import Any

import pytest

from groundscribe.provenance.redaction import Redactor


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("call it with sk-live-0123456789abcdefghij please", "sk-live-0123456789abcdefghij"),
        ("Authorization: Bearer eyJhbGciOi.J9abc-def_123", "eyJhbGciOi.J9abc-def_123"),
        ("aws key AKIAIOSFODNN7EXAMPLE in the config", "AKIAIOSFODNN7EXAMPLE"),
        ("password=hunter2 was the whole problem", "hunter2"),
        ('config {"openai_api_key": "abc123xyz"}', "abc123xyz"),
    ],
)
def test_known_secret_shapes_are_removed_from_text(
    redactor: Redactor, text: str, secret: str
) -> None:
    """Recognisable credential shapes never survive into a persisted string."""
    redacted = redactor.redact_text(text)
    assert secret not in redacted
    assert "REDACTED" in redacted


def test_private_key_blocks_are_removed_entirely(redactor: Redactor) -> None:
    """A PEM block is redacted as a unit, not line by line."""
    text = (
        "here is the deploy key:\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAsecretmaterialhere\n"
        "-----END RSA PRIVATE KEY-----\n"
        "use it wisely"
    )
    redacted = redactor.redact_text(text)
    assert "secretmaterialhere" not in redacted
    assert "BEGIN RSA PRIVATE KEY" not in redacted
    # The surrounding prose — the part with provenance value — is untouched.
    assert "here is the deploy key:" in redacted
    assert "use it wisely" in redacted


def test_confidential_markers_remove_the_marked_span_only(redactor: Redactor) -> None:
    """Authors can mark source material confidential; only the span is dropped."""
    text = "public intro [[CONFIDENTIAL]]unreleased revenue figures[[/CONFIDENTIAL]] public outro"
    redacted = redactor.redact_text(text)
    assert "unreleased revenue figures" not in redacted
    assert redacted == "public intro [REDACTED:confidential] public outro"


def test_registered_literal_secrets_are_removed_even_without_a_pattern() -> None:
    """A configured secret is removed even when it looks like ordinary text.

    Pattern matching alone cannot recognise, say, an internal hostname or a
    passphrase; the runtime therefore registers the values it knows are secret.
    """
    redactor = Redactor(secrets=["correct horse battery staple"])
    redacted = redactor.redact_text("the passphrase is correct horse battery staple, obviously")
    assert "correct horse battery staple" not in redacted
    assert redacted.startswith("the passphrase is ")


def test_ordinary_prose_is_left_byte_for_byte_intact(redactor: Redactor) -> None:
    """Redaction must not corrupt the payload it is protecting."""
    text = "The p99 latency dropped from 240ms to 90ms after the cache change."
    assert redactor.redact_text(text) == text


def test_sensitive_keys_are_redacted_by_name_whatever_the_value(redactor: Redactor) -> None:
    """A value under a sensitive key is redacted even if its shape is unremarkable."""
    payload = {"model": "gpt-x", "api_key": "plainlookingvalue", "authorization": 12345}
    redacted = redactor.redact_payload(payload)

    assert redacted["model"] == "gpt-x"
    assert "plainlookingvalue" not in str(redacted)
    # The keys survive: the record still shows that a key was supplied.
    assert set(redacted) == {"model", "api_key", "authorization"}
    assert redacted["api_key"] == "[REDACTED:api_key]"
    assert redacted["authorization"] == "[REDACTED:authorization]"


def test_payload_redaction_recurses_into_nested_structures(redactor: Redactor) -> None:
    """Secrets hide in nested message lists; redaction must reach them."""
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "my key is sk-live-0123456789abcdefghij"},
        ],
        "provider_config": {"headers": {"Authorization": "Bearer topsecrettoken123"}},
        "retries": 2,
    }

    redacted = redactor.redact_payload(payload)

    flattened = str(redacted)
    assert "sk-live-0123456789abcdefghij" not in flattened
    assert "topsecrettoken123" not in flattened
    # Structure and non-secret content are preserved.
    assert redacted["retries"] == 2
    assert redacted["messages"][0]["content"] == "be terse"
    assert redacted["messages"][1]["role"] == "user"


def test_redaction_does_not_mutate_the_caller_payload(redactor: Redactor) -> None:
    """The redactor returns a new structure; the in-memory original is untouched.

    Callers still hold the live request they are about to send — redaction is a
    persistence-boundary concern, not an in-flight mutation.
    """
    payload = {"api_key": "sk-live-0123456789abcdefghij"}
    redactor.redact_payload(payload)
    assert payload["api_key"] == "sk-live-0123456789abcdefghij"


def test_redaction_is_idempotent(redactor: Redactor) -> None:
    """Re-redacting stored content must not corrupt the placeholders."""
    once = redactor.redact_text("password=hunter2 and sk-live-0123456789abcdefghij")
    assert redactor.redact_text(once) == once


def test_non_string_scalars_pass_through_unchanged(redactor: Redactor) -> None:
    """Numbers, booleans and nulls carry no secrets and must survive typed."""
    payload: dict[str, Any] = {"temperature": 0.2, "stream": False, "seed": None, "tags": ["a"]}
    assert redactor.redact_payload(payload) == payload
