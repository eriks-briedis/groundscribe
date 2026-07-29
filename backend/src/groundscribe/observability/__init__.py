"""What an operator can see about a running installation (phase 14).

plan/14 → *Observability surface*: the metrics the spec names, and structured
logs that correlate project / article / pipeline run / stage execution / job /
model request / tool invocation / trace event.

Deliberately a *reading* layer. Nothing here records anything: the provenance
recorder (phase 03) is the only writer of execution history, and a second one
here would create a set of numbers that could disagree with the trace they claim
to describe. Everything in this package derives from rows that already exist.
"""

from __future__ import annotations

from groundscribe.observability.metrics import METRIC_NAMES, RunMetrics, collect_metrics

__all__ = ["METRIC_NAMES", "RunMetrics", "collect_metrics"]
