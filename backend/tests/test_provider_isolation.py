"""Architectural test: provider SDK types stay out of the domain (phase 04).

Spec (plan/04 → Exit criteria): *provider SDK types do not appear in the domain
layer*. That is a property of the import graph, not of any single call, so it is
checked by reading the source rather than by exercising behaviour — the failure
mode is a well-meaning import added months from now, which no functional test
would notice.

Two rules, both directional:

1. Only ``groundscribe.llm.adapters`` may name a provider SDK. Everything else —
   including the rest of ``groundscribe.llm`` — talks to the protocol.
2. The domain, provenance and storage layers do not import ``groundscribe.llm``
   at all. Provenance records what a model call did; it must not depend on the
   thing that makes the call.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import groundscribe

SOURCE_ROOT = Path(groundscribe.__file__).resolve().parent

#: Provider SDK distributions that may only be imported behind an adapter.
PROVIDER_SDKS = frozenset({"openai", "anthropic", "ollama", "httpx", "requests"})

#: Layers that must not depend on the LLM layer.
DOMAIN_PACKAGES = ("domain", "provenance", "storage")


def _modules(package: str = "") -> list[Path]:
    root = SOURCE_ROOT / package if package else SOURCE_ROOT
    return sorted(root.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    """Every top-level module name ``path`` imports, absolute or relative."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(path: Path) -> set[str]:
    """Every dotted module name ``path`` imports from (absolute imports only)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", _modules(), ids=lambda p: str(p.relative_to(SOURCE_ROOT)))
def test_only_adapters_may_import_a_provider_sdk(module: Path) -> None:
    leaked = _imported_roots(module) & PROVIDER_SDKS
    if module.parent.name == "adapters":
        return
    assert not leaked, f"{module.relative_to(SOURCE_ROOT)} imports provider SDKs {sorted(leaked)}"


@pytest.mark.parametrize("package", DOMAIN_PACKAGES)
def test_the_domain_layers_do_not_import_the_llm_layer(package: str) -> None:
    offenders = {
        str(module.relative_to(SOURCE_ROOT))
        for module in _modules(package)
        if any(name.startswith("groundscribe.llm") for name in _imported_modules(module))
    }
    assert not offenders, f"{package} must not depend on groundscribe.llm: {sorted(offenders)}"
