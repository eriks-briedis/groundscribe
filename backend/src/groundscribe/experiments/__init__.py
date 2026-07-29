"""Experimentation, replay and evaluation (phase 12).

Turning the provenance substrate into something that can answer "did that change
make it better?" — replay and fork over recorded executions, experiments over
datasets built from approved work, and the metrics that compare them.
"""

from groundscribe.experiments.edit_distance import (
    ManualEditDistance,
    RubricSignal,
    measure_manual_edit,
    rubric_signal,
)
from groundscribe.experiments.replay import Rerun, plan_rerun, rerun_of
from groundscribe.experiments.variables import ForkRequest, ForkVariable, ForkVariables

__all__ = [
    "ForkRequest",
    "ForkVariable",
    "ForkVariables",
    "ManualEditDistance",
    "Rerun",
    "RubricSignal",
    "measure_manual_edit",
    "plan_rerun",
    "rerun_of",
    "rubric_signal",
]
