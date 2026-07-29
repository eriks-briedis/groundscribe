"""The personal voice system (phase 10).

plan/10 replaces generic humanisation with something a person owns: a structured
profile of operational instructions, held at three scopes, carrying strengths
that say how firmly each one binds.

The parts, and why they are separate:

- :mod:`~groundscribe.voice.schemas` — what a voice *is*. Immutable documents, so
  an article that names the profile version it was written under has said
  something true.
- :mod:`~groundscribe.voice.precedence` — which instruction wins where, with the
  source of each one recorded rather than merged away.
- :mod:`~groundscribe.voice.calibration` — proposing a first profile from what a
  person recognises, since nobody can write their own style guide cold.
- :mod:`~groundscribe.voice.learning` — inferring rules from repeated edits, and
  refusing to apply them without being asked.
- :mod:`~groundscribe.voice.repetition` — noticing when a voice has become a
  template, which is the failure mode of doing all of the above well.
"""

from __future__ import annotations

from groundscribe.voice.enums import InstructionStrength, VoiceCategory, VoiceScope
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument

__all__ = [
    "InstructionStrength",
    "VoiceCategory",
    "VoiceInstruction",
    "VoiceProfileDocument",
    "VoiceScope",
]
