"""The voice every article is written under before anyone sets one (phase 16).

Phase 10 built the voice system and shipped it empty: ``VoiceProfileDocument()``
with no instructions, resolved for every project that had not run calibration.
Every consumer behaved correctly given that input and every one of them was
therefore inert — the align-voice prompt rendered its three headings over an
empty list, the scorer was asked whether prose matched a profile that said
nothing and returned 94, and final validation asked what the profile prohibited
and got ``()``.

An empty profile is not a neutral starting point. It is a claim that the author
has no habits, and the model fills the gap with its own — which is the only
outcome the phase existed to prevent.

**Loaded, not hard-coded.** The rules are prose a person has to be able to read,
argue with and edit without a Python change, so they live in
``config/voice-profile.yaml`` beside the rubric and the routing policy. A missing
or broken file is an error rather than a silent fall back to empty: falling back
would restore exactly the state this module exists to end, and would do it
quietly.

**Cached per path.** The file is read once per process. It is config, not
content, and re-reading it per stage would let one run be written under two
different voices.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from groundscribe.paths import config_root
from groundscribe.voice.schemas import VoiceProfileDocument

VOICE_PROFILE_FILENAME = "voice-profile.yaml"


class VoiceProfileError(Exception):
    """The shipped voice profile is missing or does not describe a voice."""


def shipped_voice_profile() -> VoiceProfileDocument:
    """The shipped profile, from the config root."""
    return _load(config_root() / VOICE_PROFILE_FILENAME)


@lru_cache(maxsize=8)
def _load(path: Path) -> VoiceProfileDocument:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VoiceProfileError(f"cannot read voice profile {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise VoiceProfileError(f"invalid YAML in voice profile {path}: {exc}") from exc
    try:
        return VoiceProfileDocument.model_validate(raw)
    except ValidationError as exc:
        raise VoiceProfileError(f"invalid voice profile {path}: {exc}") from exc


__all__ = ["VOICE_PROFILE_FILENAME", "VoiceProfileError", "shipped_voice_profile"]
