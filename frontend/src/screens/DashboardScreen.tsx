/**
 * The project dashboard (phase 11).
 *
 * plan/11 → *status, source completeness, proposed articles, current stage,
 * active jobs, unresolved questions, revision counts, approval state, recent
 * failures, token/cost summaries*.
 *
 * All of it is here, but not all of it is equal, and the old arrangement said it
 * was: eight panels of identical weight, led by a state name in the pipeline's
 * own vocabulary. A person opening this screen has one question — *is it my
 * move?* — and the answer is now the first thing on it, in a sentence the
 * backend wrote for a person. The counts, the failures and the cost are what
 * they always were: the record, below the fold, in the order you would ask for
 * them.
 *
 * Artefact-first, which here means: what the project *has* comes before what the
 * system did. The counts are the backend's, the failures are the ones it
 * recorded, the phases are the ones it published, and the actions are the ones it
 * offered — this screen adds a layout and nothing else.
 */
import { useEffect, useState } from 'react';

import { fetchDashboard, subscribeToJob, type Dashboard } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { ActionBar, PendingCommand, Stranded } from '@/components/ActionBar';
import { Privacy } from '@/components/Privacy';
import { JourneyStrip, NowCard } from '@/components/Journey';
import { RoutingProfilePanel } from '@/components/RoutingProfile';

function money(value: number | null | undefined): string {
  return value === null || value === undefined ? 'not reported' : `$${value}`;
}

/**
 * One counted thing, with the count first.
 *
 * A row of these rather than a sentence of numbers: the counts are what a person
 * scans for, and a sentence makes them read the words to find them.
 */
function Stat({ id, value, label }: { id: string; value: string | number; label: string }) {
  return (
    <span className="stat" data-testid={`stat-${id}`}>
      <span className="stat__value">{value}</span>
      <span className="stat__label">{label}</span>
    </span>
  );
}

export interface DashboardScreenProps {
  projectId: string;
  actor?: string;
}

export function DashboardScreen({ projectId, actor = 'ada' }: DashboardScreenProps) {
  const resource = useResource<Dashboard>(() => fetchDashboard(projectId), [projectId]);

  return (
    <Loaded resource={resource}>
      {(dashboard) => {
        const questions = dashboard.questions ?? [];
        const reload = () => resource.reload();

        return (
          <section className="screen screen--dashboard">
            <header className="screen__header">
              <h1>{dashboard.project.title}</h1>
              {dashboard.project.description ? (
                <p className="screen__subtitle">{dashboard.project.description}</p>
              ) : null}
              {/* The machine's own name for where the run is. Kept, because this
                  is a tool whose promise is that you can check it — and demoted
                  to a footnote, because it is not what a person came to read. */}
              <p className="mono muted" data-testid="run-state" title="the run's workflow state">
                {dashboard.state.replace(/_/g, ' ')}
              </p>
            </header>

            <div className="journey">
              <JourneyStrip journey={dashboard.journey} />
              <NowCard journey={dashboard.journey}>
                {/* The state's own commands, inside the card that explains the
                    state: a button is easier to trust when the sentence above it
                    says what it will do. */}
                <PendingCommand command={dashboard.pending_command} onCommand={reload} />
                <Stranded command={dashboard.retry_command} actor={actor} onCommand={reload} />
                <ActionBar links={dashboard.action_links ?? []} actor={actor} onCommand={reload} />
                {questions.length ? (
                  <p className="muted">
                    <a href={`#/projects/${projectId}/questions`}>
                      {questions.length} question{questions.length === 1 ? '' : 's'} waiting for you
                    </a>
                  </p>
                ) : null}
              </NowCard>
            </div>

            <JobProgress jobs={dashboard.active_jobs ?? []} onFinished={reload} />

            <div className="panel__grid">
              <section className="panel">
                <h2>Source</h2>
                <p className="stats" data-testid="source-completeness">
                  <Stat id="documents" value={dashboard.source.documents} label="documents" />
                  <Stat id="segments" value={dashboard.source.segments} label="segments" />
                  <Stat id="claims" value={dashboard.source.claims} label="claims" />
                  <Stat
                    id="open-questions"
                    value={dashboard.source.unresolved_questions}
                    label="open questions"
                  />
                  <Stat
                    id="answered-questions"
                    value={dashboard.source.answered_questions}
                    label="answered"
                  />
                </p>
                {dashboard.source.confidential_documents > 0 ? (
                  <p className="warning">
                    {dashboard.source.confidential_documents} confidential document(s) in scope
                  </p>
                ) : null}
                <p className="muted">
                  <a href={`#/projects/${projectId}/source`}>Open the source workspace</a>
                </p>
              </section>

              <section className="panel">
                <h2>Cost</h2>
                <p className="stats" data-testid="usage">
                  <Stat id="calls" value={dashboard.usage.model_calls} label="model calls" />
                  <Stat id="tokens-in" value={dashboard.usage.input_tokens} label="tokens in" />
                  <Stat id="tokens-out" value={dashboard.usage.output_tokens} label="tokens out" />
                  <Stat id="cost" value={money(dashboard.usage.cost_usd)} label="spent" />
                </p>
                <p className="muted">
                  <a href={`#/projects/${projectId}/trace`}>Every call, in order</a>
                </p>
              </section>

              <section className="panel" data-testid="privacy-panel">
                <h2>Trace</h2>
                <Privacy projectId={projectId} privacy={dashboard.privacy} />
              </section>

              <section className="panel">
                <h2>Voice</h2>
                <p className="muted">
                  The rules every draft is written, edited and scored against.
                </p>
                <p className="muted">
                  <a href={`#/projects/${projectId}/voice`}>Read and edit them</a>
                </p>
              </section>
            </div>

            <section className="panel">
              <h2>Articles</h2>
              {(dashboard.articles ?? []).length ? (
                <ul className="cards">
                  {(dashboard.articles ?? []).map((article) => (
                    <li key={article.id} className="card" data-testid={`article-${article.id}`}>
                      <a className="card__title" href={`#/articles/${article.id}`}>
                        {article.title}
                      </a>
                      <p className="muted">
                        {article.versions} versions · {article.rewrite_rounds} rewrites ·{' '}
                        {article.open_findings} blocking findings
                      </p>
                      <p className="muted">
                        {article.latest_score
                          ? `scored ${article.latest_score.overall} (${
                              article.latest_score.passed ? 'passed' : 'failed'
                            })`
                          : 'not scored yet'}
                        {article.validated === null || article.validated === undefined
                          ? ''
                          : article.validated
                            ? ' · validated'
                            : ' · validation failed'}
                      </p>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="empty">
                  No articles yet. They are created when you approve an architecture.
                </p>
              )}
            </section>

            <section className="panel">
              <h2>Questions</h2>
              {questions.length ? (
                <ul className="cards">
                  {questions.map((question) => (
                    <li key={question.id} className="card">
                      <a href={`#/projects/${projectId}/questions`}>{question.question}</a>
                      <p>
                        <span className={`tag${question.priority === 'blocking' ? ' tag--blocking' : ''}`}>
                          {question.priority}
                        </span>
                      </p>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="empty">Nothing open. The source answered everything it was asked.</p>
              )}
            </section>

            {/* Below the failures deliberately. This is where a person looks
                *after* something went wrong in a way the pipeline cannot fix for
                them — a stage that will not fit its input — and putting it above
                the record would present a configuration knob as part of the
                normal reading order. */}
            <section className="panel">
              <h2>Recent failures</h2>
              {(dashboard.recent_failures ?? []).length ? (
                <ul className="cards">
                  {/* A run carries its failures with it, so one that has since
                      been answered has to say so — read at the moment somebody
                      is asking why nothing is happening, it otherwise looks
                      like the reason. */}
                  {(dashboard.recent_failures ?? []).map((failure) => (
                    <li
                      key={failure.execution_id}
                      className="card"
                      data-superseded={failure.superseded ? 'yes' : undefined}
                    >
                      <a href={`#/executions/${failure.execution_id}`}>{failure.stage}</a>{' '}
                      {failure.superseded ? (
                        <span className="tag tag--resolved">ran again since, and worked</span>
                      ) : null}
                      <p className="muted">{failure.error_message}</p>
                      <p className="muted">{new Date(failure.occurred_at).toLocaleString()}</p>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="empty">Nothing has failed.</p>
              )}
            </section>

            <RoutingProfilePanel profiles={dashboard.routing} actor={actor} onChanged={reload} />
          </section>
        );
      }}
    </Loaded>
  );
}

interface JobProgressProps {
  jobs: NonNullable<Dashboard['active_jobs']>;
  onFinished: () => void;
}

/**
 * What the worker is doing, live.
 *
 * plan/11 → *SSE for progress*. The stream is the backend's; this listens to the
 * job the dashboard was told is running and shows the frames as they arrive. It
 * draws no conclusion from them beyond "the job ended, so re-read the page" —
 * what the run's state became is the backend's answer, not a guess from a frame.
 *
 * Nothing is rendered when nothing is running. An empty "Work in flight" panel
 * on every idle screen is a panel a reader learns to skip, and the state of the
 * run is already said above, in words.
 */
function JobProgress({ jobs, onFinished }: JobProgressProps) {
  const job = jobs[0];
  const [frames, setFrames] = useState<string[]>([]);

  useEffect(() => {
    setFrames([]);
    if (!job) return undefined;
    return subscribeToJob(job.id, ({ event, data }) => {
      const detail = String(data.detail ?? data.status ?? data.stage ?? event);
      setFrames((seen) => [...seen, detail]);
      // Any status frame is a reason to re-read the page, and *what* the status
      // means is not this side's question: the backend answers it in the next
      // response. Comparing against "succeeded" here would be the beginning of a
      // second opinion about where the run is.
      if (data.status !== undefined) onFinished();
    });
  }, [job?.id]);

  if (!job) return null;

  return (
    <section className="panel" data-testid="work-in-flight">
      <h2>
        Work in flight <span className="pill pill--pipeline">{job.status}</span>
      </h2>
      <p className="muted">{job.job_type.replace(/_/g, ' ')}</p>
      <ul className="progress">
        {frames.map((frame, index) => (
          <li key={`${frame}-${index}`} className="muted">
            {frame}
          </li>
        ))}
      </ul>
    </section>
  );
}
