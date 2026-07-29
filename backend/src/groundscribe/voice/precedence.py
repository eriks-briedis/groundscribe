"""Resolving three scopes into one effective voice (phase 10).

plan/10 → *global user profile < project profile < article override; resolver
produces the effective instruction set and records the source + version of each
active instruction.*

Two design choices carry the module.

**Resolution is per instruction, not per profile.** An article override replaces
the instructions it names and inherits the rest. Replacing the whole profile
would force a person editing one line of tone to restate every rule they still
wanted — and the rules they forgot to restate would disappear silently, which is
the worst possible failure for a system whose subject is what your writing must
never do.

**Nothing is merged away.** Every active instruction keeps the profile, scope and
version it came from, and one that beat another keeps the loser. The resolver's
output is not a set of instructions; it is a record of a decision about
instructions, and the difference is what lets a person ask why the prose reads
the way it does and where to go to change it.
"""

from __future__ import annotations

from dataclasses import dataclass

from groundscribe.voice.enums import VoiceScope
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument


@dataclass(frozen=True)
class ActiveInstruction:
    """One instruction in force, and where it came from.

    ``overrides`` is the instruction this one displaced, if any — kept rather
    than discarded because precedence is a decision, and a decision with no
    record cannot be told apart from an accident.
    """

    instruction: VoiceInstruction
    scope: VoiceScope
    profile_name: str
    profile_version: str
    overrides: ActiveInstruction | None = None

    @property
    def source(self) -> str:
        """How the source is written into a trace: ``name@version (scope)``."""
        return f"{self.profile_name}@{self.profile_version} ({self.scope.value})"


@dataclass(frozen=True)
class ResolvedVoice:
    """The effective instruction set, with the reasoning behind it intact."""

    active: tuple[ActiveInstruction, ...] = ()
    suppressed: tuple[ActiveInstruction, ...] = ()
    sources: tuple[str, ...] = ()

    @property
    def profile(self) -> VoiceProfileDocument:
        """The resolution as a profile the phase-07 stages can consume.

        Versioned by naming its sources — ``ada@7 + this-one@2`` — rather than
        with a number of its own. A synthetic version would be a value nobody
        could look up; this one says exactly which documents produced it, which
        is what an article recording "written under voice X" needs X to be.
        """
        narrowest = max(
            (active.scope for active in self.active),
            default=VoiceScope.GLOBAL,
            key=lambda s: s.precedence,
        )
        return VoiceProfileDocument(
            name="effective",
            version=" + ".join(self.sources) or "empty",
            scope=narrowest,
            description="Resolved from " + (", ".join(self.sources) or "no profile"),
            instructions=tuple(active.instruction for active in self.active),
        )

    def record(self) -> list[dict[str, str]]:
        """The resolution as plain data, for a decision record or an API response."""
        return [
            {
                "instruction_id": active.instruction.id,
                "category": active.instruction.category.value,
                "strength": active.instruction.strength.value,
                "source": active.source,
                "overrides": active.overrides.source if active.overrides else "",
            }
            for active in self.active
        ]


def resolve_voice(
    *,
    global_profile: VoiceProfileDocument | None = None,
    project_profile: VoiceProfileDocument | None = None,
    article_profile: VoiceProfileDocument | None = None,
) -> ResolvedVoice:
    """Combine the profiles in force into one effective voice.

    Applied widest first, so a narrower instruction displaces the one it shares
    an id with and keeps it as ``overrides``. Suppressions are applied last, and
    only after every scope has had its say: a project profile may reinstate what
    the global one declared, and an article may then drop it, in that order.

    Any of the three may be absent — a person who has not set a voice still gets
    to write. plan/10's calibration produces the first profile, and requiring one
    before anything could run would make onboarding a precondition rather than a
    first result.
    """
    layers = [
        layer for layer in (global_profile, project_profile, article_profile) if layer is not None
    ]

    resolved: dict[str, ActiveInstruction] = {}
    suppressed: dict[str, ActiveInstruction] = {}

    for profile in layers:
        scope = profile.scope
        for instruction in profile.instructions:
            resolved[instruction.id] = ActiveInstruction(
                instruction=instruction,
                scope=scope,
                profile_name=profile.name,
                profile_version=profile.version,
                overrides=resolved.get(instruction.id),
            )
        for dropped in profile.suppresses:
            # Suppressing something nobody declared is untidy, not broken: an
            # override outliving the rule it relaxed should not break the
            # article that carries it.
            if dropped in resolved:
                previous = resolved.pop(dropped)
                suppressed[dropped] = ActiveInstruction(
                    instruction=previous.instruction,
                    scope=scope,
                    profile_name=profile.name,
                    profile_version=profile.version,
                    overrides=previous,
                )

    return ResolvedVoice(
        active=tuple(resolved.values()),
        suppressed=tuple(suppressed.values()),
        sources=tuple(f"{profile.name}@{profile.version}" for profile in layers),
    )


__all__ = ["ActiveInstruction", "ResolvedVoice", "resolve_voice"]
