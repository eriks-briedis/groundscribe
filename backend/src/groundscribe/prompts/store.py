"""Versioned prompt templates on disk (phase 04).

Layout, fixed by plan/04::

    prompts/<template_id>/metadata.yaml
    prompts/<template_id>/v1.jinja2
    prompts/<template_id>/v2.jinja2

Prompts are the highest-leverage, most-frequently-edited part of the system, and
plan/04 forbids embedding them as strings in code. Files make a change reviewable
in a diff; a version per file makes "which prompt produced this artefact?"
answerable months later, when the current template no longer resembles the one
that ran.

Rendering is strict in both directions: a declared variable that was not supplied
is refused, and a name the template uses but nobody declared raises rather than
rendering an empty span. Jinja's permissive default would produce a
plausible-looking prompt with a hole in it — invisible in the output *and* in the
record, which is the worst failure mode available to a system whose product is
provenance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from groundscribe.provenance.schemas import EffectiveRequest, Message, ToolDefinition

METADATA_FILENAME = "metadata.yaml"
TEMPLATE_SUFFIX = ".jinja2"


class PromptTemplateError(Exception):
    """A prompt could not be loaded or rendered.

    One error type for missing families, unknown versions, metadata that
    disagrees with the files, and render failures: to a caller they are all "this
    prompt cannot be produced", and the message says which.
    """


class PromptVersionSpec(BaseModel):
    """One version of a template, as declared in ``metadata.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str = ""
    required_variables: tuple[str, ...] = ()
    output_schema_version: int | None = None
    notes: str = ""


class PromptMetadata(BaseModel):
    """A template family: its versions and which one is current.

    ``current_version`` is declared, never inferred from the highest file on
    disk. A new version has to be able to exist — reviewed, diffed, evaluated —
    without being promoted the moment it lands.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    description: str = ""
    current_version: str
    versions: dict[str, PromptVersionSpec]


class RenderedPrompt(BaseModel):
    """A rendered template plus everything provenance needs about the rendering."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    version: str
    variables: dict[str, Any] = Field(default_factory=dict)
    rendered_prompt: str
    messages: tuple[Message, ...] = ()
    output_schema_version: int | None = None

    def to_effective_request(
        self,
        *,
        provider_config: dict[str, Any] | None = None,
        tool_definitions: tuple[ToolDefinition, ...] = (),
        output_schema: dict[str, Any] | None = None,
        extra_messages: tuple[Message, ...] = (),
    ) -> EffectiveRequest:
        """Turn this rendering into the request record phase 03 stores.

        ``extra_messages`` are appended after the template's own — that is how
        the repair ladder adds validation feedback without every stage template
        having to know the ladder exists.
        """
        return EffectiveRequest(
            template_id=self.template_id,
            template_version=self.version,
            rendered_prompt=self.rendered_prompt,
            template_variables=dict(self.variables),
            messages=[*self.messages, *extra_messages],
            tool_definitions=list(tool_definitions),
            output_schema=output_schema,
            output_schema_version=self.output_schema_version,
            provider_config=provider_config or {},
        )


class PromptStore:
    """Loads and renders versioned prompt templates from a directory tree.

    Expects ``<root>/<template_id>/metadata.yaml`` beside ``vN.jinja2`` files.
    Metadata is cached per family: templates are read from disk at render time so
    a local edit takes effect, while the declaration of what exists is stable for
    the life of the store.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        # autoescape stays off: these render prompts, not HTML. Escaping would
        # corrupt the very characters (quotes, braces) a JSON-shaped prompt needs.
        self._env = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._families: dict[str, PromptMetadata] = {}

    def metadata(self, template_id: str) -> PromptMetadata:
        """The declared metadata for one template family."""
        cached = self._families.get(template_id)
        if cached is not None:
            return cached

        path = self._root / template_id / METADATA_FILENAME
        if not path.is_file():
            raise PromptTemplateError(f"no prompt template {template_id!r} under {self._root}")
        try:
            declared = PromptMetadata.model_validate(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
        except (ValidationError, yaml.YAMLError) as exc:
            raise PromptTemplateError(f"invalid prompt metadata in {path}: {exc}") from exc
        if declared.template_id != template_id:
            raise PromptTemplateError(
                f"{path} declares template_id {declared.template_id!r} "
                f"but lives in directory {template_id!r}"
            )
        self._families[template_id] = declared
        return declared

    def versions(self, template_id: str) -> tuple[str, ...]:
        """The declared versions of a family, in declaration order."""
        return tuple(self.metadata(template_id).versions)

    def render(
        self, template_id: str, variables: dict[str, Any], *, version: str | None = None
    ) -> RenderedPrompt:
        """Render one version of a template and capture how it was rendered."""
        declared = self.metadata(template_id)
        resolved = version or declared.current_version
        spec = declared.versions.get(resolved)
        if spec is None:
            raise PromptTemplateError(
                f"prompt {template_id!r} has no version {resolved!r} "
                f"(declared: {', '.join(declared.versions) or 'none'})"
            )

        missing = [name for name in spec.required_variables if name not in variables]
        if missing:
            raise PromptTemplateError(
                f"prompt {template_id}/{resolved} requires {', '.join(missing)}"
            )

        body = self._render(
            self._template_text(template_id, resolved), variables, f"{template_id}/{resolved}"
        )
        messages: tuple[Message, ...] = ()
        if spec.system:
            system = self._render(spec.system, variables, f"{template_id}/{resolved} (system)")
            messages += (Message(role="system", content=system),)
        messages += (Message(role="user", content=body),)

        return RenderedPrompt(
            template_id=template_id,
            version=resolved,
            variables=dict(variables),
            rendered_prompt=body,
            messages=messages,
            output_schema_version=spec.output_schema_version,
        )

    def _template_text(self, template_id: str, version: str) -> str:
        path = self._root / template_id / f"{version}{TEMPLATE_SUFFIX}"
        if not path.is_file():
            raise PromptTemplateError(
                f"prompt {template_id!r} declares version {version!r} but {path} is missing"
            )
        return path.read_text(encoding="utf-8")

    def _render(self, source: str, variables: dict[str, Any], where: str) -> str:
        """Render one string, turning any Jinja failure into a prompt error.

        Callers are stages and the repair ladder; neither can act on a Jinja
        traceback, and both need to know *which* template failed.
        """
        try:
            return self._env.from_string(source).render(**variables)
        except TemplateError as exc:
            raise PromptTemplateError(f"failed to render {where}: {exc}") from exc
