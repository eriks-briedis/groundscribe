"""``writer``: the same commands as the API, from a terminal (phase 09).

plan/09 → *Typer CLI mirroring the spec's commands ... all delegating to the
service layer*.

Every command here does three things: read arguments, call one service method,
print what came back. It decides nothing. That is not a style preference — the
plan requires the CLI to share the service layer rather than reimplement the
workflow, and the cheapest way to guarantee it is for this module never to
mention the workflow at all. It does not import it, and a test asserts that it
cannot.

The service comes from a module-level factory rather than being constructed per
command, so a test can substitute a double and a deployment can point the same
binary at a different database by setting one environment variable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer

from groundscribe.api.openapi import contract_app, export_schema
from groundscribe.app.bootstrap import build_runtime
from groundscribe.app.handlers import stage_handlers
from groundscribe.app.services import ApplicationService
from groundscribe.domain.enums import AnswerResponse, ArticleDepth, SourceFormat
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.experiments.reproducibility import contract
from groundscribe.experiments.runs import ArmSpec
from groundscribe.experiments.variables import ForkVariables
from groundscribe.jobs.worker import Worker
from groundscribe.observability.logging import configure_logging
from groundscribe.privacy.export import ExportFormat
from groundscribe.voice.schemas import VoiceProfileDocument

app = typer.Typer(help="groundscribe — a local-first, inspectable editorial workflow.")
project_app = typer.Typer(help="Create and inspect projects.")
source_app = typer.Typer(help="Import source material and build the source model.")
architecture_app = typer.Typer(help="Propose and approve the shape of the work.")
article_app = typer.Typer(help="Draft, review, rewrite and publish one article.")
execution_app = typer.Typer(help="Inspect, replay and fork execution records.")
experiment_app = typer.Typer(help="Compare executions and open experiments.")
voice_app = typer.Typer(help="Manage the personal voice profile.")
worker_app = typer.Typer(help="Run the background worker.")
privacy_app = typer.Typer(help="See where material goes, and export or forget a trace.")
contracts_app = typer.Typer(help="Generate the API contract.")

app.add_typer(project_app, name="project")
app.add_typer(source_app, name="source")
app.add_typer(architecture_app, name="architecture")
app.add_typer(article_app, name="article")
app.add_typer(execution_app, name="execution")
app.add_typer(experiment_app, name="experiment")
app.add_typer(voice_app, name="voice")
app.add_typer(worker_app, name="worker")
app.add_typer(privacy_app, name="privacy")
app.add_typer(contracts_app, name="contracts")


def default_service() -> ApplicationService:
    """A service over the local installation."""
    return ApplicationService(build_runtime())


#: How every command gets its service. A module attribute rather than a call
#: inside each command, so a test can substitute a double once instead of
#: patching every command that might use one.
service_factory: Callable[[], ApplicationService] = default_service


@contextmanager
def _command() -> Iterator[ApplicationService]:
    """One invocation, one transaction.

    A command a person can re-run is the unit that must either have happened or
    not, so the boundary is here rather than inside the service. Without it the
    CLI would appear to work — every command printing the state it produced —
    and persist nothing at all.
    """
    service = service_factory()
    try:
        yield service
    except Exception:
        service.rollback()
        raise
    service.commit()


def _emit(result: Any) -> None:
    """Print what a command produced, in the shape a terminal can read."""
    state = getattr(result, "state", None)
    if state is None:
        typer.echo(getattr(result, "id", result))
        return
    typer.echo(f"state: {state.value}")
    typer.echo(f"actions: {', '.join(result.available_actions)}")
    if result.job is not None:
        typer.echo(f"job: {result.job.id} ({result.job.status.value})")


# ----------------------------------------------------------------------
# Projects and sources
# ----------------------------------------------------------------------


@project_app.command("create")
def project_create(
    title: Annotated[str, typer.Option(help="What the project is about.")],
    author: Annotated[str, typer.Option(help="Who is writing it.")],
    audience: Annotated[str, typer.Option(help="Who it is written for.")],
    platform: Annotated[str, typer.Option(help="Where it will be published.")],
    depth: Annotated[ArticleDepth, typer.Option(help="How deep it should go.")],
    provider: Annotated[
        list[str] | None,
        typer.Option(help="A provider permitted to see this project's material."),
    ] = None,
    words: Annotated[int | None, typer.Option(help="Target length in words.")] = None,
    description: Annotated[str, typer.Option()] = "",
) -> None:
    """Open a project and the run behind it."""
    with _command() as service:
        _emit(
            service.create_project(
                title=title,
                author_id=author,
                description=description,
                constraints=EditorialConstraints(
                    audience=audience,
                    platform=platform,
                    depth=depth,
                    target_length_words=words,
                    allowed_providers=tuple(provider or ()),
                ),
            )
        )


@project_app.command("show")
def project_show(project: str) -> None:
    """Where a run is, and what may be done to it."""
    with _command() as service:
        _emit(service.project_state(project))


@source_app.command("import")
def source_import(
    project: str,
    title: Annotated[str, typer.Option(help="What the source document is called.")],
    file: Annotated[Path, typer.Option(help="The file to read.")],
    source_format: Annotated[SourceFormat, typer.Option("--format")] = SourceFormat.MARKDOWN,
    confidential: Annotated[bool, typer.Option()] = False,
) -> None:
    """Store one piece of source material."""
    with _command() as service:
        _emit(
            asyncio.run(
                service.import_source(
                    project,
                    title=title,
                    text=file.read_text(encoding="utf-8"),
                    source_format=source_format,
                    confidential=confidential,
                    uri=None,
                )
            )
        )


@source_app.command("extract")
def source_extract(
    project: str,
    token_budget: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Queue the source-model build and the gap analysis after it."""
    with _command() as service:
        _emit(asyncio.run(service.extract_source_model(project, token_budget=token_budget)))


@source_app.command("answer")
def source_answer(
    project: str,
    gap: Annotated[str, typer.Option(help="Which question is being answered.")],
    by: Annotated[str, typer.Option(help="Who is answering.")],
    text: Annotated[str, typer.Option()] = "",
    response: Annotated[AnswerResponse, typer.Option()] = AnswerResponse.ANSWERED,
) -> None:
    """Answer a surfaced question and rebuild the source model."""
    with _command() as service:
        _emit(service.answer_gap(project, gap_id=gap, text=text, answered_by=by, response=response))


# ----------------------------------------------------------------------
# Architecture and articles
# ----------------------------------------------------------------------


@architecture_app.command("propose")
def architecture_propose(project: str) -> None:
    """Queue a proposal of the article or series the source supports."""
    with _command() as service:
        _emit(asyncio.run(service.propose_architecture(project)))


@architecture_app.command("approve")
def architecture_approve(
    project: str, by: Annotated[str, typer.Option(help="Who is approving.")]
) -> None:
    """Lock the architecture and open an article per concept."""
    with _command() as service:
        _emit(service.approve_architecture(project, approved_by=by))


@article_app.command("brief")
def article_brief(article: str) -> None:
    """Queue the brief this article is drafted against."""
    with _command() as service:
        _emit(asyncio.run(service.generate_brief(article)))


@article_app.command("draft")
def article_draft(article: str) -> None:
    """Queue the first draft."""
    with _command() as service:
        _emit(asyncio.run(service.draft(article)))


@article_app.command("review")
def article_review(article: str) -> None:
    """Queue a substantive review of the current version."""
    with _command() as service:
        _emit(asyncio.run(service.review(article)))


@article_app.command("plan")
def article_plan(article: str) -> None:
    """Queue the revision plan a rewrite will be bound by."""
    with _command() as service:
        _emit(asyncio.run(service.plan_revision(article)))


@article_app.command("rewrite")
def article_rewrite(article: str) -> None:
    """Queue the rewrite."""
    with _command() as service:
        _emit(asyncio.run(service.rewrite(article)))


@article_app.command("voice")
def article_voice(article: str) -> None:
    """Queue the voice pass."""
    with _command() as service:
        _emit(asyncio.run(service.voice_align(article)))


@article_app.command("score")
def article_score(article: str) -> None:
    """Queue the scoring pass."""
    with _command() as service:
        _emit(asyncio.run(service.score(article)))


@article_app.command("validate")
def article_validate(article: str) -> None:
    """Run the final deterministic checks now."""
    with _command() as service:
        _emit(asyncio.run(service.validate(article)))


@article_app.command("export")
def article_export(
    article: str, by: Annotated[str, typer.Option(help="Who is approving.")]
) -> None:
    """Approve and publish the validated article.

    Named ``export`` because that is what the spec calls it from a person's side.
    What publishing *produces* — formats, redaction, destinations — is phase 13;
    here it is the approval that makes an article publishable.
    """
    with _command() as service:
        _emit(service.approve(article, approved_by=by))


# ----------------------------------------------------------------------
# Executions and experiments
# ----------------------------------------------------------------------


@execution_app.command("inspect")
def execution_inspect(execution: str) -> None:
    """One execution: which stage ran, under which build, and how it ended."""
    with _command() as service:
        record = service.get_execution(execution)
        typer.echo(f"{record.id} {record.stage} {record.impl_version} {record.status.value}")


@execution_app.command("replay")
def execution_replay(
    execution: str, by: Annotated[str, typer.Option(help="Who asked for it.")]
) -> None:
    """Queue the stage again exactly as it ran; the original is untouched."""
    with _command() as service:
        rerun = service.replay_execution(execution, requested_by=by)
        typer.echo(f"queued {rerun.job.id} to replay {rerun.source_execution_id}")


@execution_app.command("fork")
def execution_fork(
    execution: str,
    by: Annotated[str, typer.Option(help="Who asked for it.")],
    reason: Annotated[str, typer.Option(help="Why this fork is worth running.")] = "",
    model: Annotated[str | None, typer.Option(help="Run it against another model.")] = None,
    provider: Annotated[str | None, typer.Option(help="…from another provider.")] = None,
    temperature: Annotated[float | None, typer.Option(help="…at another temperature.")] = None,
    prompt_version: Annotated[str | None, typer.Option(help="…or another prompt version.")] = None,
) -> None:
    """Run the stage again with something changed (phase 12).

    One option per variable rather than a free-form mapping: the vocabulary is
    closed, and a typo should be refused by the command line rather than by a
    worker three seconds later.
    """
    with _command() as service:
        rerun = service.fork_execution(
            execution,
            requested_by=by,
            reason=reason,
            variables=ForkVariables(
                model=model,
                provider=provider,
                temperature=temperature,
                prompt_version=prompt_version,
            ),
        )
        typer.echo(f"queued {rerun.job.id} to fork {rerun.source_execution_id}")


@experiment_app.command("compare")
def experiment_compare(left: str, right: str) -> None:
    """Put two executions side by side."""
    with _command() as service:
        first, second = service.compare_executions(left, right)
        typer.echo(f"{first.id} {first.stage} {first.status.value}")
        typer.echo(f"{second.id} {second.stage} {second.status.value}")


@experiment_app.command("dataset")
def experiment_dataset(
    name: str,
    created_by: Annotated[str, typer.Option(help="Who is building the corpus.")],
    description: Annotated[str, typer.Option()] = "",
    include: Annotated[
        list[str] | None,
        typer.Option(help="A sensitive project to let in, by id. Repeatable."),
    ] = None,
) -> None:
    """Build an evaluation corpus out of the runs a person approved."""
    with _command() as service:
        dataset = service.build_dataset(
            name=name,
            created_by=created_by,
            description=description,
            include_sensitive=include or (),
        )
        typer.echo(f"{dataset.id} {len(dataset.entries)} example(s)")


@experiment_app.command("create")
def experiment_create(
    name: str,
    dataset: Annotated[str, typer.Option(help="The corpus to run over.")],
    created_by: Annotated[str, typer.Option(help="Who is asking.")],
    arm: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "An arm as label=variable=value, or just a label for the baseline. "
                "Repeatable; the first arm is the baseline."
            )
        ),
    ] = None,
) -> None:
    """Open an experiment over one corpus, with the configurations to compare."""
    with _command() as service:
        typer.echo(
            service.create_experiment(
                name=name,
                dataset_id=dataset,
                created_by=created_by,
                arms=_arms(arm or []),
            ).id
        )


@experiment_app.command("start")
def experiment_start(experiment: str) -> None:
    """Queue every arm against every example."""
    with _command() as service:
        results = service.start_experiment(experiment)
        typer.echo(f"queued {len(results)} run(s)")


@experiment_app.command("report")
def experiment_report(experiment: str) -> None:
    """The aggregate table, one row per arm."""
    with _command() as service:
        for row in service.experiment_report(experiment).comparison:
            marker = "*" if row.baseline else " "
            typer.echo(
                f"{marker} {row.label}: {row.completed}/{row.examples} completed, "
                f"pass {_number(row.pass_rate)}, preference {_number(row.human_preference)}, "
                f"cost {_number(row.total_cost_usd)}"
            )


@experiment_app.command("prefer")
def experiment_prefer(
    experiment: str,
    entry: Annotated[str, typer.Option(help="Which example was judged.")],
    arm: Annotated[str, typer.Option(help="Which arm did better.")],
    decided_by: Annotated[str, typer.Option(help="Who judged it.")],
    reason: Annotated[str, typer.Option()] = "",
) -> None:
    """Record which arm a person judged better on one example."""
    with _command() as service:
        typer.echo(
            service.prefer_arm(
                experiment, entry_id=entry, arm_id=arm, decided_by=decided_by, reason=reason
            ).id
        )


@experiment_app.command("reproducibility")
def experiment_reproducibility() -> None:
    """What repeating work here does and does not guarantee."""
    for item in contract():
        typer.echo(f"{'yes' if item.promised else 'NO '} {item.title}")
        typer.echo(f"    {item.detail}")


def _arms(specs: list[str]) -> list[ArmSpec]:
    """Parse ``label`` / ``label=variable=value`` into arms, the first as baseline.

    Deliberately thin. A richer syntax would be a second way to express what the
    fork vocabulary already expresses, and the two would disagree about what a
    candidate is.
    """
    arms: list[ArmSpec] = []
    for ordinal, spec in enumerate(specs):
        label, _, assignment = spec.partition("=")
        variable, _, value = assignment.partition("=")
        arms.append(
            ArmSpec(
                label=label,
                baseline=ordinal == 0,
                variables=(
                    ForkVariables.model_validate({variable: value}) if variable else ForkVariables()
                ),
            )
        )
    return arms


def _number(value: float | None) -> str:
    """A figure, or the fact that there was nothing to measure."""
    return "n/a" if value is None else f"{value:g}"


# ----------------------------------------------------------------------
# Voice
# ----------------------------------------------------------------------


@voice_app.command("save")
def voice_save(
    user: Annotated[str, typer.Option(help="Whose voice this is.")],
    file: Annotated[Path, typer.Option(help="A profile document, as JSON.")],
    project: Annotated[str | None, typer.Option()] = None,
    article: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Put a profile version in force at its scope.

    Read from a file rather than assembled from flags. A profile is a document a
    person edits, versions and keeps — five categories of instruction with
    strengths — and reconstructing one from command-line options would be a
    worse editor than the one they already have.
    """
    with _command() as service:
        document = VoiceProfileDocument.model_validate_json(file.read_text(encoding="utf-8"))
        saved = service.save_voice_profile(
            document, user_id=user, project_id=project, article_id=article
        )
        typer.echo(f"{saved.id} {saved.scope.value} {saved.version}")


@voice_app.command("show")
def voice_show(project: str, article: Annotated[str | None, typer.Option()] = None) -> None:
    """The voice in force here, and where each instruction came from."""
    with _command() as service:
        resolved = service.effective_voice(
            user_id=service.author_of(project), project_id=project, article_id=article
        )
        for entry in resolved.record():
            typer.echo(f"{entry['instruction_id']}  [{entry['strength']}]  {entry['source']}")


@voice_app.command("suggestions")
def voice_suggestions(user: Annotated[str, typer.Option()]) -> None:
    """Inferred rules waiting for an answer. Listing applies nothing."""
    with _command() as service:
        for suggestion in service.voice_suggestions(user_id=user):
            occurrences = suggestion.evidence.get("occurrences", "?")
            typer.echo(f"{suggestion.id}  {suggestion.habit}  ({occurrences} edits)")


@voice_app.command("approve")
def voice_approve(
    suggestion: str,
    by: Annotated[str, typer.Option(help="Who is approving.")],
    version: Annotated[str, typer.Option(help="The version this creates.")],
) -> None:
    """Make an inferred rule permanent. The only command that changes a voice."""
    with _command() as service:
        saved = service.approve_voice_suggestion(suggestion, approved_by=by, version=version)
        typer.echo(f"{saved.id} {saved.version}")


@voice_app.command("reject")
def voice_reject(
    suggestion: str,
    by: Annotated[str, typer.Option(help="Who is declining.")],
    reason: Annotated[str, typer.Option()] = "",
) -> None:
    """Decline an inferred rule, keeping the reason."""
    with _command() as service:
        declined = service.reject_voice_suggestion(suggestion, rejected_by=by, reason=reason)
        typer.echo(f"{declined.id} {declined.status.value}")


# ----------------------------------------------------------------------
# Operating the system
# ----------------------------------------------------------------------


@worker_app.command("run")
def worker_run(
    worker_id: Annotated[str, typer.Option(help="Name this worker reports itself as.")] = "worker",
) -> None:
    """Recover anything a previous worker abandoned, then drain the queue.

    Outside ``_command`` because a worker is not one transaction: each job is
    its own, committed as it finishes, so a crash halfway through a batch keeps
    what the earlier jobs did.
    """
    # The one command that runs unattended, so the one that has to leave a log
    # somebody can read afterwards (plan/14). Every other command reports to the
    # person who typed it.
    configure_logging()
    runtime = build_runtime()
    worker = Worker(
        queue=runtime.queue,
        recorder=runtime.recorder,
        handlers=stage_handlers(runtime),
        worker_id=worker_id,
    )
    recovered = worker.recover()
    runtime.session.commit()
    for job in recovered.reclaimed:
        typer.echo(f"reclaimed {job.id}")
    for execution in recovered.orphaned:
        typer.echo(f"orphaned execution {execution.id} ({execution.stage})")

    done = asyncio.run(worker.run_until_idle())
    runtime.session.commit()
    typer.echo(f"ran {len(done)} job(s)")


@contracts_app.command("export")
def contracts_export(
    path: Annotated[Path | None, typer.Option(help="Where to write the schema.")] = None,
) -> None:
    """Regenerate the OpenAPI contract phase 11 builds its client from."""
    written = (
        export_schema(contract_app(), path=path)
        if path is not None
        else export_schema(contract_app())
    )
    typer.echo(str(written))


# ----------------------------------------------------------------------
# Privacy and export (phase 13)
# ----------------------------------------------------------------------


@article_app.command("render")
def article_render(
    version: str,
    fmt: Annotated[ExportFormat, typer.Option("--format")] = ExportFormat.MARKDOWN,
    out: Annotated[Path | None, typer.Option(help="Write here instead of stdout.")] = None,
) -> None:
    """Render one validated article version in a publishable format.

    Addressed by version rather than by article: what a person publishes is the
    version that passed validation, and naming it is what makes rendering the
    wrong one impossible rather than merely unlikely.
    """
    with _command() as service:
        exported = service.render_version(version, fmt)
    if out is not None:
        out.write_text(exported.content, encoding="utf-8")
        typer.echo(f"{out} ({exported.format.value}, {exported.content_hash})")
    else:
        typer.echo(exported.content)


@privacy_app.command("visibility")
def privacy_visibility(project: str) -> None:
    """Which provider sees this project's material, and what is kept of it."""
    with _command() as service:
        surface = service.provider_visibility(project)
    where = "leaves this machine" if surface.leaves_this_machine else "stays on this machine"
    typer.echo(f"{surface.project_id}: {where}; retention {surface.retention_mode.value}")
    typer.echo(
        f"  {surface.segments_sent} segment(s) sent, {surface.segments_withheld} withheld; "
        f"{surface.confidential_segments} confidential, {surface.internal_segments} internal"
    )
    for stage in surface.stages:
        locality = "local" if stage.local else "external"
        allowed = "permitted" if stage.permitted else "NOT PERMITTED"
        typer.echo(f"  {stage.stage}: {stage.provider}/{stage.model} ({locality}, {allowed})")


@privacy_app.command("traces")
def privacy_traces(
    project: str,
    out: Annotated[Path | None, typer.Option(help="Write here instead of stdout.")] = None,
    sanitise: Annotated[bool, typer.Option(help="Withhold every stored payload.")] = False,
    report: Annotated[bool, typer.Option(help="Render for a person rather than a tool.")] = False,
    yes_i_know: Annotated[
        bool,
        typer.Option(
            "--i-know-this-may-contain-confidential-material",
            help="Required for a full export of a project holding confidential material.",
        ),
    ] = False,
) -> None:
    """Export this project's execution records.

    The flag is spelled out at length on purpose. It is the last thing between
    confidential material and a file, and an option called ``--force`` would be
    typed by reflex.
    """
    with _command() as service:
        exported = service.export_traces(
            project, sanitise=sanitise, confidential_material_acknowledged=yes_i_know
        )
    for warning in exported.warnings:
        typer.echo(f"warning: {warning}", err=True)
    body = exported.to_report() if report else exported.to_json()
    if out is not None:
        out.write_text(body, encoding="utf-8")
        typer.echo(f"{out} ({len(exported.runs)} run(s), {exported.withheld_payloads} withheld)")
    else:
        typer.echo(body)


@privacy_app.command("report")
def privacy_report(
    project: Annotated[str | None, typer.Argument(help="One project, or all of them.")] = None,
) -> None:
    """What the trace costs, and what deduplication already saved.

    The breakdown is the useful part: a total prompts no decision, and "most of
    it is raw provider payloads" prompts exactly one.
    """
    with _command() as service:
        report = service.storage_report(project)
    scope = report.project_id or "all projects"
    typer.echo(
        f"{scope}: {report.total_bytes} byte(s) in {report.snapshots} artefact(s); "
        f"{report.stored_bytes} on disk across {report.distinct_blobs} blob(s) "
        f"({report.deduplicated_bytes} saved by deduplication)"
    )
    for kind, usage in sorted(report.by_type.items(), key=lambda item: -item[1].bytes):
        typer.echo(f"  {kind}: {usage.count} artefact(s), {usage.bytes} byte(s)")


@privacy_app.command("forget")
def privacy_forget(project: str) -> None:
    """Delete this project's stored payloads, keeping the record of what ran."""
    with _command() as service:
        removed = service.delete_traces(project)
    typer.echo(
        f"{removed.project_id}: {removed.payloads} payload(s) removed, "
        f"{removed.bytes_reclaimed} byte(s); {removed.shared_payloads} kept (shared), "
        f"{removed.records_kept} call(s) still recorded"
    )


def run() -> None:  # pragma: no cover - the console-script entry point
    app()


__all__ = ["app", "default_service", "run", "service_factory"]
