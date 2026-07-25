"""The editorial pipeline stages (phases 06-08).

Source ingestion, source-of-truth extraction, gap questioning, content
architecture and brief generation live here (phase 06); drafting, review, voice
alignment, scoring and final validation join them in phases 07 and 08. Every one
of them implements the single contract in :mod:`groundscribe.stages.base`.
"""

from __future__ import annotations

from groundscribe.stages.base import PipelineContext, PipelineStage, StageResult, StageRunner

__all__ = ["PipelineContext", "PipelineStage", "StageResult", "StageRunner"]
