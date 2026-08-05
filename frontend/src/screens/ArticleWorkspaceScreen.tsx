/**
 * The article workspace, and the approval view inside it (phase 11).
 *
 * plan/11 → *brief, current version, source evidence, reviewer findings,
 * revision plan, voice rules, previous version, diff, scores, available actions,
 * producing execution, branch lineage* — and, before publishing, *the full
 * required context*.
 *
 * The approval panel is part of this screen rather than a page of its own,
 * because it is this screen with nothing hidden. A separate page would be a
 * second answer to "which version is being approved", and the two could differ.
 */
import { useState } from 'react';

import {
  ApiError,
  fetchArticleWorkspace,
  sendCommand,
  type ArticleWorkspace,
} from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { ActionBar, PendingCommand } from '@/components/ActionBar';
import { DiffViewer } from '@/components/DiffViewer';
import { Disclosure, Payload } from '@/components/Disclosure';
import { Export } from '@/components/Export';
import { LineageGraph } from '@/components/LineageGraph';
import { Markdown } from '@/components/Markdown';
import { Rerun } from '@/components/Rerun';
import { ScoreTable } from '@/components/ScoreTable';

function readable(value: string): string {
  return value.replace(/_/g, ' ');
}

export interface ArticleWorkspaceScreenProps {
  articleId: string;
  actor: string;
}

export function ArticleWorkspaceScreen({ articleId, actor }: ArticleWorkspaceScreenProps) {
  const resource = useResource<ArticleWorkspace>(
    () => fetchArticleWorkspace(articleId),
    [articleId],
  );
  const [approving, setApproving] = useState(false);

  return (
    <Loaded resource={resource}>
      {(workspace) => (
        <section className="screen screen--article">
          <header className="screen__header">
            <h1>{workspace.article.title}</h1>
            <p className="state" data-testid="run-state">
              {readable(workspace.state)}
            </p>
            {workspace.producing_execution ? (
              <p className="muted">
                produced by{' '}
                <a href={`#/executions/${workspace.producing_execution.id}`}>
                  {workspace.producing_execution.stage}
                </a>{' '}
                ({workspace.producing_execution.impl_version})
              </p>
            ) : null}
          </header>

          {workspace.producing_execution ? (
            <section className="panel" data-testid="rerun-version">
              <h2>Run it again</h2>
              <Rerun
                command={workspace.producing_execution.rerun_command}
                forkCommand={workspace.producing_execution.fork_command}
                stage={workspace.producing_execution.stage}
                feedsPipeline={workspace.producing_execution.rerun_feeds_pipeline}
                actor={actor}
                onQueued={resource.reload}
              />
            </section>
          ) : null}

          <ActionBar
            links={workspace.action_links ?? []}
            actor={actor}
            onCommand={() => resource.reload()}
          />
          <PendingCommand command={workspace.pending_command} onCommand={() => resource.reload()} />

          <section className="panel">
            <h2>Current version</h2>
            {workspace.current_version ? (
              <>
                <p className="muted">
                  v{workspace.current_version.ordinal} · {workspace.current_version.thesis}
                </p>
                <Markdown body={workspace.current_version.body ?? ''} data-testid="version-body" />
              </>
            ) : (
              <p>Nothing has been drafted yet.</p>
            )}
          </section>

          {workspace.current_version ? (
            <section className="panel" data-testid="export-version">
              <h2>Export</h2>
              <p className="muted">
                The version that passed validation, rendered. The version id and content hash
                travel with it, so a file on a desktop can still say which run produced it.
              </p>
              <Export
                versionId={workspace.current_version.id}
                title={workspace.article.title}
              />
            </section>
          ) : null}

          <section className="panel">
            <h2>Changed since the last version</h2>
            <DiffViewer diff={workspace.diff} />
          </section>

          <section className="panel" data-testid="brief">
            <h2>Brief</h2>
            <Payload label="the contract this was written to" value={workspace.brief} />
            {workspace.brief ? <p>{String(workspace.brief.thesis ?? '')}</p> : null}
          </section>

          <section className="panel">
            <h2>Findings</h2>
            <ul className="findings">
              {(workspace.findings ?? []).map((finding) => (
                <li key={finding.id} data-testid={`finding-${finding.id}`} className="card">
                  <p>
                    <span className="tag">{finding.severity}</span> {finding.description}
                  </p>
                  <p className="muted">
                    {finding.location} · {finding.status}
                    {finding.blocks_publication ? ' · blocks publication' : ''}
                  </p>
                  {finding.evidence ? <p>{finding.evidence}</p> : null}
                  {finding.recommended_correction ? (
                    <p className="muted">suggested: {finding.recommended_correction}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>

          <section className="panel" data-testid="source-evidence">
            <h2>Source evidence</h2>
            {workspace.source_evidence?.length ? (
              <ul className="claims">
                {workspace.source_evidence.map((claim) => (
                  <li key={claim.id}>
                    {claim.text} <span className="tag">{claim.classification}</span>
                    <span className="muted"> ({(claim.segment_ids ?? []).join(', ')})</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p>
                No claim is cited yet.{' '}
                <a href={`#/projects/${workspace.article.project_id}/source`}>Source workspace</a>
              </p>
            )}
          </section>

          <section className="panel">
            <h2>Revision plan</h2>
            {workspace.revision_plan ? (
              <>
                <p>{String(workspace.revision_plan.summary ?? '')}</p>
                <Payload label="the plan in full" value={workspace.revision_plan} />
              </>
            ) : (
              <p>No revision has been planned.</p>
            )}
          </section>

          <section className="panel" data-testid="voice">
            <h2>Voice</h2>
            <p className="muted">{(workspace.voice.sources ?? []).join(' + ') || 'no profile'}</p>
            <ul>
              {(workspace.voice.active ?? []).map((instruction) => (
                <li key={instruction.instruction_id}>
                  <span className="tag">{readable(instruction.strength)}</span>{' '}
                  {instruction.instruction || instruction.instruction_id}{' '}
                  <span className="muted">({instruction.source})</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="panel">
            <h2>Scores</h2>
            <ScoreTable scores={workspace.scores ?? []} />
          </section>

          <section className="panel">
            <h2>Lineage</h2>
            <LineageGraph graph={workspace.lineage} />
          </section>

          <section className="panel">
            <h2>Approval</h2>
            <button type="button" onClick={() => setApproving((value) => !value)}>
              {approving ? 'hide what approving means' : 'ready to approve?'}
            </button>
            {approving ? <Approval workspace={workspace} /> : null}
          </section>

          <RouteRevision workspace={workspace} actor={actor} onDone={resource.reload} />
          <ContinueToNext workspace={workspace} actor={actor} onDone={resource.reload} />
        </section>
      )}
    </Loaded>
  );
}

/**
 * Everything plan/11 requires a person to see before publishing.
 *
 * Listed rather than summarised. Each of these is something an interface could
 * fold away to look calmer, and the calm version is the one that gets an article
 * approved without its remaining concerns being read.
 */
/**
 * Sending a refused score back to be corrected, and choosing which way.
 *
 * The pause at `revision_required` is deliberate — it is where a person may
 * accept the article anyway. This is the other way out, and the choice inside it
 * matters more than it looks: `factual_gap` routes to re-extraction by default,
 * which is right when the facts exist and extraction missed them, and a loop
 * when nobody has ever written them down. Re-extracting the same source with the
 * same answers produces the same source model.
 *
 * The destinations are not listed here. The screen offers "correct it" and
 * "ask me what is missing"; the policy decides what each means for this failure
 * and refuses a preference it does not permit.
 */
function RouteRevision({
  workspace,
  actor,
  onDone,
}: {
  workspace: ArticleWorkspace;
  actor: string;
  onDone: () => void;
}) {
  const link = workspace.revise_command;
  const [problem, setProblem] = useState('');
  const [busy, setBusy] = useState(false);

  if (!link?.path) return null;

  async function send(prefer?: string) {
    if (!link?.path) return;
    setBusy(true);
    setProblem('');
    try {
      await sendCommand(link.path, prefer ? { actor_id: actor, prefer } : { actor_id: actor });
      onDone();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" data-testid="route-revision">
      <h2>Send it back</h2>
      <p className="muted">
        The score refused this article. Correcting it is the policy&apos;s call — these say
        which kind of correction, not where it goes.
      </p>
      <div className="actions">
        <button type="button" onClick={() => send()} disabled={busy}>
          Correct it against the source
        </button>
        <button
          type="button"
          onClick={() => send('source_questions_required')}
          disabled={busy}
        >
          Ask me what is missing
        </button>
      </div>
      <p className="muted">
        Choose the second when the source never said it. Re-reading the same material cannot
        find a fact nobody wrote down.
      </p>
      {problem ? (
        <p className="failure" role="alert">
          {problem}
        </p>
      ) : null}
    </section>
  );
}

/**
 * Approving this article and starting another the architecture approved.
 *
 * Approving an architecture opens an article per approved concept and the run
 * carries exactly one of them to publication; the rest were rows nothing could
 * act on, because the finished state is terminal and artefacts are scoped to the
 * run that produced them (phase 16).
 *
 * Rendered only when the backend offers `approve_and_continue`, and only for
 * siblings with nothing written yet — an article that already has versions is
 * not what "another one" means.
 *
 * The action needs a second article id, which no generic action bar can supply,
 * so it is offered here rather than beside the other buttons: the link says the
 * option exists, this screen builds the request.
 */
function ContinueToNext({
  workspace,
  actor,
  onDone,
}: {
  workspace: ArticleWorkspace;
  actor: string;
  onDone: () => void;
}) {
  const link = workspace.continue_command;
  const unwritten = (workspace.siblings ?? []).filter((sibling) => sibling.versions === 0);
  const [next, setNext] = useState('');
  const [problem, setProblem] = useState('');

  if (!link?.path || unwritten.length === 0) return null;

  async function go() {
    if (!link?.path || !next) return;
    setProblem('');
    try {
      await sendCommand(link.path, { actor_id: actor, next_article_id: next });
      onDone();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    }
  }

  return (
    <section className="panel" data-testid="continue-to-next">
      <h2>Write another</h2>
      <p className="muted">
        This project&apos;s architecture approved {unwritten.length + 1} articles. Publishing this
        one need not end the run.
      </p>
      <label>
        Next article
        <select value={next} onChange={(event) => setNext(event.target.value)}>
          <option value="">Finish here</option>
          {unwritten.map((sibling) => (
            <option key={sibling.id} value={sibling.id}>
              {sibling.title}
            </option>
          ))}
        </select>
      </label>
      <button type="button" onClick={go} disabled={!next}>
        Approve this and start the next
      </button>
      {problem ? (
        <p className="failure" role="alert">
          {problem}
        </p>
      ) : null}
    </section>
  );
}

function Approval({ workspace }: { workspace: ArticleWorkspace }) {
  const approval = workspace.approval;
  const validation = workspace.validation;
  const score = (workspace.scores ?? []).at(-1);

  return (
    <div className="approval" data-testid="approval">
      <p>
        {approval.rewrite_rounds} rewrite round(s) ·{' '}
        {score ? `scored ${score.overall} (${score.passed ? 'passed' : 'failed'})` : 'not scored'} ·{' '}
        {validation
          ? validation.passed
            ? 'validation passed'
            : 'validation failed'
          : 'not validated'}
      </p>

      <h3>Remaining concerns</h3>
      <ul>
        {(approval.remaining_concerns ?? []).map((concern, index) => (
          <li key={index}>{concern}</li>
        ))}
        {(approval.remaining_concerns ?? []).length === 0 ? <li>None recorded.</li> : null}
      </ul>

      <h3>Interventions</h3>
      <ul>
        {(approval.interventions ?? []).map((intervention) => (
          <li key={intervention.id}>
            {readable(intervention.intervention_type)} by {intervention.user_id} ·{' '}
            {intervention.occurred_at}
          </li>
        ))}
      </ul>

      <h3>Models and prompts</h3>
      <ul>
        {(approval.model_versions ?? []).map((version, index) => (
          <li key={index}>
            {version.stage}: {version.provider}/{version.model} · {version.template_id}@
            {version.template_version}
          </li>
        ))}
      </ul>

      <h3>Cost</h3>
      <p>
        {approval.usage.model_calls} calls · {approval.usage.input_tokens} in ·{' '}
        {approval.usage.output_tokens} out ·{' '}
        {approval.usage.cost_usd === null || approval.usage.cost_usd === undefined
          ? 'cost not reported'
          : `$${approval.usage.cost_usd}`}
      </p>

      <Disclosure summary="validation report">
        <ul>
          {(validation?.checks_run ?? []).map((check) => (
            <li key={check}>{check}</li>
          ))}
        </ul>
        <Payload label="findings" value={validation?.findings ?? []} />
      </Disclosure>

      <p>
        <a href={`#/articles/${workspace.article.id}/reviews`}>Review history</a> ·{' '}
        <a href={`#/projects/${workspace.article.project_id}/trace`}>Full trace</a>
      </p>
    </div>
  );
}
