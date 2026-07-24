"""Execution provenance: how every artefact was actually produced (phase 03).

A distinct subsystem from the editorial artefacts of phase 02 and from
operational logs. Provenance here is structured domain data — typed rows with
foreign keys — not an unstructured event blob, so a reviewer can ask "which
prompt, which model call, which tool result, which decision produced this
paragraph?" and get an answer by query rather than by log grepping
(plan/00 → observable provenance is part of the product).
"""

from __future__ import annotations
