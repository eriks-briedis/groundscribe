"""How a strength actually binds the voice pass (phase 10).

Spec (plan/10 → Test-first specification): *a hard rule (e.g. "no em dashes",
"never use the internal product name") is enforced by the voice pass; a tendency
is not applied as a mandatory template.*

Those are two different kinds of claim and the tests treat them differently.

The first is checkable and is checked: after the pass returns, the prose is
compared against every term the profile forbids, and a violation stops the
article. A hard rule that lived only in the prompt would be a hard rule the
system hopes for, and hope does not survive a model having a bad day.

The second is a claim about what the system *does not* do, which is harder to
pin. It is tested where the difference is observable: in what the stage sends. A
tendency travels as a tendency — labelled, with its strength — rather than being
promoted into a rule or silently checked like one. Enforcing tendencies is how
plan/10's named risk, style homogenisation, would actually arrive.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.stages.base import StageRunner
from groundscribe.stages.errors import VoiceRuleViolation
from groundscribe.stages.voice import VOICE_STAGE, AlignVoice, check_hard_rules
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.voice.enums import InstructionStrength, VoiceCategory
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument
from groundscribe.workflow.states import WorkflowAction
from test_drafting import Drafted, draft
from test_voice import golden_voice_pass

NO_EM_DASH = VoiceInstruction(
    id="no-em-dash",
    category=VoiceCategory.PUNCTUATION,
    strength=InstructionStrength.HARD_RULE,
    text="Never use an em dash; use a colon, or split the sentence.",
    prohibits=("—",),
)

NO_INTERNAL_NAME = VoiceInstruction(
    id="no-internal-name",
    category=VoiceCategory.PROHIBITED_PATTERNS,
    strength=InstructionStrength.HARD_RULE,
    text="Never use the internal product name in published writing.",
    prohibits=("Rivet",),
)

OPEN_CONCRETE = VoiceInstruction(
    id="open-concrete",
    category=VoiceCategory.STRUCTURE,
    strength=InstructionStrength.TENDENCY,
    text="Usually open on a concrete incident rather than a definition.",
)

#: For the pure checker: every rule, whatever the golden prose happens to contain.
RULES = VoiceProfileDocument(
    name="ada", version="3", instructions=(NO_EM_DASH, NO_INTERNAL_NAME, OPEN_CONCRETE)
)

#: For the stage: the golden draft legitimately uses an em dash, so a profile
#: banning one would fail every test here for a reason none of them is about.
#: The internal product name appears nowhere in it, which is what makes it a
#: rule the tests can break deliberately.
STRICT = VoiceProfileDocument(
    name="ada", version="3", instructions=(NO_INTERNAL_NAME, OPEN_CONCRETE)
)


async def align(
    db_session: Session,
    snapshot_store: SnapshotStore,
    *,
    payload: dict[str, Any] | None = None,
    voice: VoiceProfileDocument = STRICT,
) -> tuple[Drafted, Any]:
    """Draft the golden article and run a voice pass under ``voice``."""
    drafted = await draft(db_session, snapshot_store)
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.model_client.script_response(
        VOICE_STAGE, payload if payload is not None else golden_voice_pass()
    )
    result = await StageRunner(drafted.context).run(
        AlignVoice(
            previous=drafted.result.value.draft,
            parent=drafted.result.value.version,
            concept=drafted.briefed.concept,
            brief=drafted.briefed.brief,
            voice=voice,
        )
    )
    return drafted, result


def violating(term: str) -> dict[str, Any]:
    """A pass whose prose breaks a hard rule, and says nothing about it."""
    payload = golden_voice_pass()
    payload["body"] = f"{payload['body']}\n\nOne more thought {term} and then we are done.\n"
    return payload


# ----------------------------------------------------------------------
# Hard rules are checked, not hoped for
# ----------------------------------------------------------------------


def test_the_checker_finds_every_rule_the_prose_breaks() -> None:
    """All of them, not the first. A person fixing one at a time is a person
    running the pass four times to learn four things."""
    broken = check_hard_rules("A thought — and the Rivet dashboard agreed.", RULES)

    assert [violation.instruction.id for violation in broken] == [
        "no-em-dash",
        "no-internal-name",
    ]
    assert broken[0].found == "—"


def test_the_checker_passes_prose_that_keeps_the_rules() -> None:
    """The ordinary case, and it must not be expensive to be right."""
    assert check_hard_rules("A thought, and the dashboard agreed.", RULES) == ()


def test_only_hard_rules_stop_an_article() -> None:
    """plan/10 → a strong preference *allows justified exceptions*.

    Checking preferences here would delete the distinction between the two
    strengths: whatever the profile said, the system would enforce both.
    """
    lenient = VoiceProfileDocument(
        name="ada",
        version="1",
        instructions=(
            VoiceInstruction(
                id="avoid-dash",
                category=VoiceCategory.PUNCTUATION,
                strength=InstructionStrength.STRONG_PREFERENCE,
                text="Prefer a colon to an em dash.",
                prohibits=("—",),
            ),
        ),
    )

    assert check_hard_rules("A thought — and then another.", lenient) == ()


async def test_a_pass_that_breaks_a_hard_rule_does_not_reach_scoring(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/10 → *a hard rule is enforced by the voice pass*.

    Enforced by stopping, because there is nothing else honest to do. The stage
    cannot rewrite the sentence — that is the model's job and it has just been
    done — and letting the version through would publish exactly what the author
    said must never appear.
    """
    with pytest.raises(VoiceRuleViolation) as raised:
        await align(db_session, snapshot_store, payload=violating("about Rivet"))

    assert "no-internal-name" in str(raised.value)
    assert "ada@3" in str(raised.value)


async def test_the_failed_pass_keeps_its_trace(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/03 → a failed execution retains everything it recorded.

    The rejected prose is the evidence. A stage that rolled back on a rule
    violation would leave a person with a failure and no way to see what the
    model actually wrote.
    """
    drafted = await draft(db_session, snapshot_store)
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.model_client.script_response(VOICE_STAGE, violating("about Rivet"))

    with pytest.raises(VoiceRuleViolation):
        await StageRunner(drafted.context).run(
            AlignVoice(
                previous=drafted.result.value.draft,
                parent=drafted.result.value.version,
                concept=drafted.briefed.concept,
                brief=drafted.briefed.brief,
                voice=STRICT,
            )
        )

    execution = drafted.context.engine.run.stage_executions[-1]
    assert execution.stage == VOICE_STAGE
    assert execution.status is ExecutionStatus.FAILED
    assert len(execution.model_invocations) == 1


# ----------------------------------------------------------------------
# Tendencies are offered, not imposed
# ----------------------------------------------------------------------


async def test_a_tendency_is_never_enforced_against_the_prose(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/10 → *tendencies: usual style, not mandatory templates*.

    The golden draft opens on a definition, which is exactly what the tendency
    discourages. It goes through anyway, because a tendency the system enforces
    is a hard rule that lied about its strength — and enforcing them is how
    plan/10's homogenisation risk actually arrives.
    """
    _, result = await align(db_session, snapshot_store)

    assert result.exit_action is WorkflowAction.SUBMIT_VOICE_PASS


async def test_the_prompt_says_how_firmly_each_instruction_binds(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A profile flattened into a list of sentences cannot express strength.

    The model has to be able to tell "never" from "usually"; sending the two
    identically and then enforcing only one is how a system ends up with prose
    that follows its tendencies religiously and breaks its rules by accident.
    """
    drafted, _ = await align(db_session, snapshot_store)
    request = drafted.model_client.last_request
    assert request is not None

    rendered = request.prompt

    assert "RULES. Never violated." in rendered
    assert "TENDENCIES." in rendered
    assert "not a template to apply" in rendered
    assert OPEN_CONCRETE.text in rendered
    assert NO_INTERNAL_NAME.text in rendered
