"""The explicit editorial workflow engine (phase 05).

plan/00 → *Explicit state machine over autonomous agents*. This package holds
the states, the transition table, the versioned policies that pick between legal
edges, and the engine that applies them while recording every decision.

The split is deliberate: :mod:`~groundscribe.workflow.machine` is pure — states,
counters and guards with no database — and
:mod:`~groundscribe.workflow.engine` wraps it with provenance. Transition rules
that needed a session to exercise would be tested through a recorder, and the
rules are the part that must be provable.
"""

from __future__ import annotations

from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import (
    TERMINAL_STATES,
    Transition,
    available_actions,
    human_pause_states,
    is_human_pause,
)

__all__ = [
    "TERMINAL_STATES",
    "Transition",
    "WorkflowAction",
    "WorkflowState",
    "available_actions",
    "human_pause_states",
    "is_human_pause",
]
