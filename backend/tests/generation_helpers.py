"""Shared construction helpers for the phase-04 LLM-contract tests.

Not a conftest, for the same reason as ``provenance_helpers``: an import error
while the subsystem is being built should fail only the modules that use it.

The prompt root is built per test from a synthetic stage template *plus a copy of
the shipped repair prompts*. Copying rather than re-inventing them means the
ladder tests exercise the real files that ship — a repair prompt that only works
in a fixture is a repair prompt that does not work.
"""

from __future__ import annotations

import shutil
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from groundscribe.llm import FakeLLMClient, LLMClient
from groundscribe.llm.generation import StructuredGenerator
from groundscribe.llm.routing import RoutingPolicy
from groundscribe.prompts import PromptStore, prompts_root
from groundscribe.provenance import models
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import make_recorder, seed_project

#: Prompt families the repair ladder itself needs, copied from the shipped set.
LADDER_TEMPLATES = ("repair", "repair_feedback")

STAGE_METADATA = """
template_id: extract_claims
description: Test stage template.
current_version: v2
versions:
  v1:
    system: You extract claims. Be literal.
    required_variables: [notes]
    output_schema_version: 1
  v2:
    system: You extract claims. Be literal and terse.
    required_variables: [notes]
    output_schema_version: 2
"""

ROUTING = """
version: "test-1"
description: Routing for the LLM-contract tests.
default:
  primary:
    provider: fake
    model: fake-default
stages:
  extract_claims:
    primary:
      provider: fake
      model: fake-strong
      temperature: 0.0
      seed: 7
      max_output_tokens: 2048
      stop_sequences: ["<<END>>"]
    fallback:
      provider: fake
      model: fake-mini
  draft_article:
    primary:
      provider: fake
      model: fake-prose
      temperature: 0.8
"""


class Grade(StrEnum):
    """A tiny closed vocabulary, so an invalid *enum* is easy to script."""

    GOOD = "good"
    BAD = "bad"


class ClaimVerdict(BaseModel):
    """The structured output the contract tests ask for."""

    model_config = ConfigDict(extra="forbid")

    claim: str
    grade: Grade


def build_prompt_root(tmp_path: Path) -> Path:
    """A prompt root holding the test stage template and the shipped ladder ones."""
    root = tmp_path / "prompts"
    stage = root / "extract_claims"
    stage.mkdir(parents=True)
    (stage / "metadata.yaml").write_text(STAGE_METADATA, encoding="utf-8")
    (stage / "v1.jinja2").write_text("Extract claims from:\n{{ notes }}", encoding="utf-8")
    (stage / "v2.jinja2").write_text("Claims only, from:\n{{ notes }}", encoding="utf-8")
    for family in LADDER_TEMPLATES:
        shutil.copytree(prompts_root() / family, root / family)
    return root


def build_routing(tmp_path: Path) -> RoutingPolicy:
    path = tmp_path / "model-routing.yaml"
    path.write_text(ROUTING, encoding="utf-8")
    return RoutingPolicy.from_yaml(path)


def build_generator(
    tmp_path: Path,
    recorder: ProvenanceRecorder,
    clients: dict[str, LLMClient],
) -> StructuredGenerator:
    return StructuredGenerator(
        clients=clients,
        recorder=recorder,
        prompts=PromptStore(build_prompt_root(tmp_path)),
        routing=build_routing(tmp_path),
    )


def started_stage(
    session: Session,
    snapshots: SnapshotStore,
    *,
    stage: str = "extract_claims",
) -> tuple[ProvenanceRecorder, models.StageExecution]:
    """A recorder with a run and a started stage execution to record against."""
    seed_project(session)
    recorder = make_recorder(session, snapshots)
    run = recorder.start_run(project_id="p1")
    return recorder, recorder.start_stage(run, stage=stage)


def fake_client(model: str = "fake-1") -> FakeLLMClient:
    return FakeLLMClient(model=model)
