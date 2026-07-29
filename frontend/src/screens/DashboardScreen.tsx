/**
 * The project dashboard (phase 11).
 *
 * plan/11 → *status, source completeness, proposed articles, current stage,
 * active jobs, unresolved questions, revision counts, approval state, recent
 * failures, token/cost summaries*.
 *
 * Artefact-first, which here means: what the project *has* comes before what the
 * system did. The counts are the backend's, the failures are the ones it
 * recorded, and the actions are the ones it offered — this screen adds a layout
 * and nothing else.
 */
import { useEffect, useState } from 'react';

import { fetchDashboard, subscribeToJob, type Dashboard } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { ActionBar, PendingCommand } from '@/components/ActionBar';

/** `human_approval_required` → `human approval required`. */
function readable(value: string): string {
  return value.replace(/_/g, ' ');
}

function money(value: number | null | undefined): string {
  return value === null || value === undefined ? 'not reported' : `$${value}`;
}

export interface DashboardScreenProps {
  projectId: string;
  actor?: string;
}

export function DashboardScreen({ projectId, actor = 'ada' }: DashboardScreenProps) {
  const resource = useResource<Dashboard>(() => fetchDashboard(projectId), [projectId]);

  return (
    <Loaded resource={resource}>
      {(dashboard) => (
        <section className="screen screen--dashboard">
          <header className="screen__header">
            <h1>{dashboard.project.title}</h1>
            <p className="screen__subtitle">{dashboard.project.description}</p>
            <p className="state" data-testid="run-state">
              {readable(dashboard.state)}
            </p>
          </header>

          <ActionBar
            links={dashboard.action_links ?? []}
            actor={actor}
            onCommand={() => resource.reload()}
          />
          <PendingCommand command={dashboard.pending_command} onCommand={() => resource.reload()} />

          <section className="panel">
            <h2>Source</h2>
            <p data-testid="source-completeness">
              {dashboard.source.documents} documents · {dashboard.source.segments} segments ·{' '}
              {dashboard.source.claims} claims · {dashboard.source.unresolved_questions} open
              questions · {dashboard.source.answered_questions} answered
            </p>
            {dashboard.source.confidential_documents > 0 ? (
              <p className="warning">
                {dashboard.source.confidential_documents} confidential document(s) in scope
              </p>
            ) : null}
          </section>

          <section className="panel">
            <h2>Articles</h2>
            <ul className="cards">
              {(dashboard.articles ?? []).map((article) => (
                <li key={article.id} className="card" data-testid={`article-${article.id}`}>
                  <a href={`#/articles/${article.id}`}>{article.title}</a>
                  <p>
                    {article.versions} versions · {article.rewrite_rounds} rewrites ·{' '}
                    {article.open_findings} blocking findings
                  </p>
                  <p>
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
          </section>

          <section className="panel">
            <h2>Questions</h2>
            <ul>
              {(dashboard.questions ?? []).map((question) => (
                <li key={question.id}>
                  <a href={`#/projects/${projectId}/questions`}>{question.question}</a>{' '}
                  <span className="tag">{question.priority}</span>
                </li>
              ))}
            </ul>
          </section>

          <JobProgress jobs={dashboard.active_jobs ?? []} onFinished={() => resource.reload()} />

          <section className="panel">
            <h2>Recent failures</h2>
            <ul>
              {(dashboard.recent_failures ?? []).map((failure) => (
                <li key={failure.execution_id}>
                  <a href={`#/executions/${failure.execution_id}`}>{failure.stage}</a>:{' '}
                  {failure.error_message}
                </li>
              ))}
            </ul>
          </section>

          <section className="panel">
            <h2>Usage</h2>
            <p data-testid="usage">
              {dashboard.usage.model_calls} calls · {dashboard.usage.input_tokens} in ·{' '}
              {dashboard.usage.output_tokens} out · {money(dashboard.usage.cost_usd)}
            </p>
            <p>
              <a href={`#/projects/${projectId}/trace`}>Execution timeline</a> ·{' '}
              <a href={`#/projects/${projectId}/source`}>Source workspace</a> ·{' '}
              <a href={`#/projects/${projectId}/architecture`}>Architecture board</a>
            </p>
          </section>
        </section>
      )}
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

  if (!job) {
    return (
      <section className="panel">
        <h2>Work in flight</h2>
        <p>Nothing is running.</p>
      </section>
    );
  }

  return (
    <section className="panel">
      <h2>Work in flight</h2>
      <p>
        {job.job_type} · {job.status}
      </p>
      <ul className="progress">
        {frames.map((frame, index) => (
          <li key={`${frame}-${index}`}>{frame}</li>
        ))}
      </ul>
    </section>
  );
}
