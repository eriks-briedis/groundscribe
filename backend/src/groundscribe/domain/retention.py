"""How much of a trace a project keeps, as a vocabulary (phase 13).

plan/13 → *Trace-retention modes*: full / redacted-full /
metadata-and-structured-only / no-raw-provider-payloads /
temporary-raw-retention / minimal-operational-logging.

Only the names live here, beside :mod:`groundscribe.domain.confidentiality` and
for the same reason: two rows carry the value — a project's constraints and every
model invocation — and a vocabulary a row stores belongs next to the rows, not
inside the module that enforces it. What each mode *keeps* is
:mod:`groundscribe.privacy.retention`, which is policy and reads this.
"""

from __future__ import annotations

from enum import StrEnum


class RetentionMode(StrEnum):
    """The six modes plan/13 names, ordered from most kept to least.

    ``FULL`` is not "unredacted": redaction before persistence is a product
    principle (plan/00), not a retention setting, and no mode can switch it off.
    ``REDACTED_FULL`` is the one that goes past that floor, removing the
    project's own restricted source material from stored payloads as well.
    """

    FULL = "full"
    REDACTED_FULL = "redacted_full"
    TEMPORARY_RAW_RETENTION = "temporary_raw_retention"
    NO_RAW_PROVIDER_PAYLOADS = "no_raw_provider_payloads"
    METADATA_AND_STRUCTURED_ONLY = "metadata_and_structured_only"
    MINIMAL_OPERATIONAL_LOGGING = "minimal_operational_logging"


__all__ = ["RetentionMode"]
