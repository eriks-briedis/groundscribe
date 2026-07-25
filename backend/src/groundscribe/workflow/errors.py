"""What the workflow refuses to do, and why (phase 05).

One exception per rejected rule rather than a single ``WorkflowError`` with a
message. A caller — phase 09's API, phase 11's UI — has to tell "you asked for
something impossible" (a bug) from "a person has to act first" (a pause) from
"this would break an invariant" (a guard), and only the first is worth a stack
trace in a log.
"""

from __future__ import annotations


class WorkflowError(Exception):
    """Base for every refusal the workflow makes."""


class IllegalTransition(WorkflowError):
    """The transition table has no such edge from the current state."""


class AmbiguousTransition(WorkflowError):
    """The action is legal but leads several places and no target was named."""


class HumanActionRequired(WorkflowError):
    """The edge exists but only a person may take it — the run is parked."""


__all__ = [
    "AmbiguousTransition",
    "HumanActionRequired",
    "IllegalTransition",
    "WorkflowError",
]
