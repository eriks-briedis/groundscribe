"""Prompt store contract tests (phase 04).

Spec (plan/04):

- prompts live under ``prompts/<stage>/vN.jinja2`` with a ``metadata.yaml``
  beside them — never as strings in code (Risks & non-goals);
- the renderer captures template id, version, input variables, rendered prompt,
  the effective message sequence and the output-schema version into the
  effective-request record;
- *changing the template version changes the recorded version*.

The store is exercised against a throwaway root so the tests state the contract
rather than the current contents of ``prompts/``; one test then asserts the
shipped directory actually satisfies it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.prompts import (
    PromptStore,
    PromptTemplateError,
    RenderedPrompt,
    prompts_root,
)

METADATA = """
template_id: greet
description: A test template.
current_version: v2
versions:
  v1:
    system: You are terse.
    required_variables: [name]
    output_schema_version: 1
  v2:
    system: You are terse, {{ name }}.
    required_variables: [name]
    output_schema_version: 2
"""


@pytest.fixture
def store(tmp_path: Path) -> PromptStore:
    family = tmp_path / "greet"
    family.mkdir()
    (family / "metadata.yaml").write_text(METADATA, encoding="utf-8")
    (family / "v1.jinja2").write_text("Say hello to {{ name }}.", encoding="utf-8")
    (family / "v2.jinja2").write_text("Greet {{ name }} in one line.", encoding="utf-8")
    return PromptStore(tmp_path)


def test_rendering_captures_id_version_inputs_prompt_and_messages(store: PromptStore) -> None:
    """Everything plan/04 lists as capturable comes back on one object.

    A renderer that returned only a string would force every caller to
    reconstruct the provenance fields, and the first caller to forget one would
    produce a record that cannot be replayed.
    """
    rendered = store.render("greet", {"name": "Ada"}, version="v1")

    assert isinstance(rendered, RenderedPrompt)
    assert (rendered.template_id, rendered.version) == ("greet", "v1")
    assert rendered.variables == {"name": "Ada"}
    assert rendered.rendered_prompt == "Say hello to Ada."
    assert [(m.role, m.content) for m in rendered.messages] == [
        ("system", "You are terse."),
        ("user", "Say hello to Ada."),
    ]
    assert rendered.output_schema_version == 1


def test_the_system_message_is_rendered_too(store: PromptStore) -> None:
    """It is a prompt like any other; leaving it un-rendered would make the
    stored message sequence differ from the one that was sent."""
    rendered = store.render("greet", {"name": "Ada"}, version="v2")
    assert rendered.messages[0].content == "You are terse, Ada."


def test_omitting_the_version_uses_the_declared_current_one(store: PromptStore) -> None:
    """Which version is current is metadata, not "the highest number on disk":
    a newer version can exist without being promoted."""
    rendered = store.render("greet", {"name": "Ada"})
    assert rendered.version == "v2"
    assert rendered.rendered_prompt == "Greet Ada in one line."


def test_changing_the_version_changes_the_recorded_version(store: PromptStore) -> None:
    """plan/04 test-first spec, stated literally."""
    first = store.render("greet", {"name": "Ada"}, version="v1").to_effective_request()
    second = store.render("greet", {"name": "Ada"}, version="v2").to_effective_request()

    assert (first.template_id, first.template_version) == ("greet", "v1")
    assert (second.template_id, second.template_version) == ("greet", "v2")
    assert first.rendered_prompt != second.rendered_prompt


def test_the_effective_request_carries_the_inputs_and_the_schema_version(
    store: PromptStore,
) -> None:
    """Inputs are part of the request record: the same template with different
    variables is a different call, and the rendered text alone cannot always
    tell you which variable produced which fragment."""
    request = store.render("greet", {"name": "Ada"}, version="v1").to_effective_request(
        provider_config={"temperature": 0.0},
        output_schema={"type": "object"},
    )

    assert request.template_variables == {"name": "Ada"}
    assert request.output_schema_version == 1
    assert request.output_schema == {"type": "object"}
    assert request.provider_config == {"temperature": 0.0}
    assert [m.role for m in request.messages] == ["system", "user"]


def test_a_missing_declared_variable_is_refused(store: PromptStore) -> None:
    """Rendering an incomplete prompt would produce a plausible-looking request
    that quietly asks the model something else."""
    with pytest.raises(PromptTemplateError, match="name"):
        store.render("greet", {}, version="v1")


def test_an_undeclared_variable_in_a_template_is_refused(tmp_path: Path) -> None:
    """StrictUndefined: a typo in a template must fail loudly, not render "" —
    an empty span in a prompt is invisible in the output and in the record."""
    family = tmp_path / "typo"
    family.mkdir()
    (family / "metadata.yaml").write_text(
        "template_id: typo\ncurrent_version: v1\nversions:\n  v1:\n"
        "    required_variables: [name]\n",
        encoding="utf-8",
    )
    (family / "v1.jinja2").write_text("Hello {{ nmae }}.", encoding="utf-8")

    with pytest.raises(PromptTemplateError):
        PromptStore(tmp_path).render("typo", {"name": "Ada"})


def test_extra_variables_are_allowed_but_still_recorded(store: PromptStore) -> None:
    """A template may ignore an input; the record must not, or a replay would
    silently drop context the caller believed it had supplied."""
    rendered = store.render("greet", {"name": "Ada", "unused": 3}, version="v1")
    assert rendered.variables == {"name": "Ada", "unused": 3}


def test_an_unknown_template_or_version_is_refused(store: PromptStore) -> None:
    with pytest.raises(PromptTemplateError, match="nosuch"):
        store.render("nosuch", {})
    with pytest.raises(PromptTemplateError, match="v9"):
        store.render("greet", {"name": "Ada"}, version="v9")


def test_a_declared_version_without_a_template_file_is_refused(tmp_path: Path) -> None:
    """Metadata and files must agree; a version that only exists in YAML would
    fail at the worst possible moment — mid-run, on the repair path."""
    family = tmp_path / "half"
    family.mkdir()
    (family / "metadata.yaml").write_text(
        "template_id: half\ncurrent_version: v1\nversions:\n  v1: {}\n", encoding="utf-8"
    )

    with pytest.raises(PromptTemplateError, match="v1"):
        PromptStore(tmp_path).render("half", {})


def test_the_store_lists_the_versions_it_holds(store: PromptStore) -> None:
    assert PromptStore.__doc__  # the store documents the layout it expects
    assert store.versions("greet") == ("v1", "v2")


def test_the_shipped_repair_prompts_load_and_render() -> None:
    """The repair ladder's own prompts are files under prompts/, not strings.

    plan/04 Risks: embedding prompts as strings in code is forbidden — and the
    ladder's prompts are the ones this phase itself owns.
    """
    store = PromptStore(prompts_root())

    feedback = store.render(
        "repair_feedback",
        {"validation_errors": ["claims.0.classification: not a valid enumeration member"]},
    )
    repair = store.render(
        "repair",
        {
            "schema_name": "ClaimSet",
            "output_schema": '{"type": "object"}',
            "previous_output": '{"claims": [{"classification": "guess"}]}',
            "validation_errors": ["claims.0.classification: not a valid enumeration member"],
        },
    )

    assert "classification" in feedback.rendered_prompt
    assert "ClaimSet" in repair.rendered_prompt
    assert repair.messages[0].role == "system"
