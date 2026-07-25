"""Where the versioned, human-editable files live (phase 04).

Prompts and routing policies are *files*, not code (plan/04 → Prompt store, and
its Risks section). Local-first means a user can open, diff and edit them, so
they sit at the repo root rather than inside the installed package.

Both roots are environment-overridable. A deployment that mounts its prompts
elsewhere should not have to fork the package to say so, and tests that build a
throwaway root pass it explicitly instead.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variables that override the defaults, in that order of priority.
PROMPTS_ROOT_ENV = "GROUNDSCRIBE_PROMPTS_ROOT"
CONFIG_ROOT_ENV = "GROUNDSCRIBE_CONFIG_ROOT"


def repo_root() -> Path:
    """The checkout root, derived from this module's location.

    ``backend/src/groundscribe/paths.py`` → three levels up. Valid for an
    editable install, which is how the project is developed and run locally; a
    packaged deployment sets the environment variables instead of relying on it.
    """
    return Path(__file__).resolve().parents[3]


def prompts_root() -> Path:
    """Directory holding ``<template_id>/vN.jinja2`` + ``metadata.yaml``."""
    override = os.environ.get(PROMPTS_ROOT_ENV)
    return Path(override) if override else repo_root() / "prompts"


def config_root() -> Path:
    """Directory holding versioned operational config (model routing, …)."""
    override = os.environ.get(CONFIG_ROOT_ENV)
    return Path(override) if override else repo_root() / "config"
