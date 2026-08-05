/**
 * The stage inspector (phase 11).
 *
 * plan/11 → *summary, inputs, context selection, effective request, raw
 * response, parsed result, tool calls, validation attempts, decisions, outputs,
 * cost/timing, errors*. Everything phase 03 recorded about one execution, in the
 * order a person debugging actually walks it.
 *
 * Every heavy payload is behind a disclosure, and the mode decides whether they
 * start open — that is the whole of plan/11's trace-overload mitigation on the
 * screen where overload is most likely.
 */
import { fetchInspection, type StageInspection } from '@/api/client';
import { useMode } from '@/app/mode';
import { Loaded, useResource } from '@/app/resource';
import { Disclosure, Payload } from '@/components/Disclosure';
import { Rerun } from '@/components/Rerun';

export interface StageInspectorScreenProps {
  executionId: string;
  actor: string;
}

export function StageInspectorScreen({ executionId, actor }: StageInspectorScreenProps) {
  const { expanded } = useMode();
  const resource = useResource<StageInspection>(
    () => fetchInspection(executionId),
    [executionId],
  );

  return (
    <Loaded resource={resource}>
      {(inspection) => (
        <section className="screen screen--inspector">
          <header className="screen__header">
            <h1>{inspection.summary.stage}</h1>
            <p className="muted">
              {inspection.summary.id} · {inspection.summary.status} ·{' '}
              {inspection.summary.impl_version} ·{' '}
              {inspection.duration_ms === null || inspection.duration_ms === undefined
                ? 'still running'
                : `${inspection.duration_ms} ms`}
            </p>
            {inspection.error ? (
              <p className="warning" role="alert">
                {inspection.error.type}: {inspection.error.message}
              </p>
            ) : null}
          </header>

          <section className="panel" data-testid="rerun">
            <h2>Run it again</h2>
            <Rerun
              command={inspection.summary.rerun_command}
              forkCommand={inspection.summary.fork_command}
              stage={inspection.summary.stage}
              actor={actor}
            />
          </section>

          <section className="panel" data-testid="inputs">
            <h2>Inputs</h2>
            {inspection.inputs?.length ? (
              inspection.inputs.map((artefact) => (
                <div key={artefact.snapshot_id}>
                  <p className="muted">
                    {artefact.artifact_type} · {artefact.role} · {artefact.content_hash}
                  </p>
                  <Payload label={artefact.snapshot_id} value={artefact.content} open={expanded} />
                </div>
              ))
            ) : (
              <p>Nothing was consumed.</p>
            )}
          </section>

          <section className="panel" data-testid="context">
            <h2>Context offered to the model</h2>
            {inspection.context_selections?.length ? (
              inspection.context_selections.map((selection) => (
                <div key={selection.id}>
                  <p className="muted">
                    {selection.strategy}@{selection.strategy_version} · budget{' '}
                    {selection.token_budget ?? 'unbounded'}
                  </p>
                  <ul>
                    {(selection.items ?? []).map((item) => (
                      <li key={`${selection.id}-${item.ordinal}`}>
                        <span className="tag">{item.disposition}</span> {item.reference}
                        {item.reason ? ` — ${item.reason}` : ''}
                      </li>
                    ))}
                  </ul>
                </div>
              ))
            ) : (
              <p>No selection was recorded.</p>
            )}
          </section>

          <section className="panel" data-testid="invocations">
            <h2>Model calls</h2>
            {inspection.invocations?.length ? (
              inspection.invocations.map((call) => (
                <article className="card" key={call.id}>
                  <h3>
                    attempt {call.attempt_ordinal} · {call.outcome}
                    {call.retry_type ? ` · ${call.retry_type.replace(/_/g, ' ')}` : ''}
                  </h3>
                  <p className="muted">
                    {call.provider}/{call.model} · {call.template_id}@{call.template_version} ·{' '}
                    {call.input_tokens} in · {call.output_tokens} out ·{' '}
                    {call.cost_usd === null || call.cost_usd === undefined
                      ? 'cost not reported'
                      : `$${call.cost_usd}`}
                  </p>
                  {call.error_message ? <p className="warning">{call.error_message}</p> : null}
                  <Payload label="effective request" value={call.effective_request} open={expanded} />
                  <Payload label="raw response" value={call.raw_response} open={expanded} />
                  <Payload label="parsed result" value={call.parsed_response} open={expanded} />
                  <Payload label="validated result" value={call.validated_response} open={expanded} />
                </article>
              ))
            ) : (
              <p>No model was called.</p>
            )}
          </section>

          <section className="panel" data-testid="tools">
            <h2>Tool calls</h2>
            {inspection.tool_calls?.length ? (
              inspection.tool_calls.map((call) => (
                <article className="card" key={call.id}>
                  <h3>
                    {call.tool_name}@{call.tool_version} · {call.status}
                  </h3>
                  <p className="muted">
                    {call.initiator.replace(/_/g, ' ')}
                    {call.approval_required ? ` · approved by ${call.approved_by ?? 'nobody'}` : ''}
                  </p>
                  <Payload label="arguments" value={call.normalised_args} open={expanded} />
                  <Payload label="result" value={call.normalised_result} open={expanded} />
                </article>
              ))
            ) : (
              <p>No tool was called.</p>
            )}
          </section>

          <section className="panel" data-testid="decisions">
            <h2>Decisions</h2>
            <ul>
              {(inspection.decisions ?? []).map((decision) => (
                <li key={decision.id}>
                  <strong>{decision.decision_type.replace(/_/g, ' ')}</strong>: {decision.outcome}
                  <p className="muted">
                    {decision.decided_by} ({decision.decided_by_type})
                    {decision.policy_version ? ` · policy ${decision.policy_version}` : ''}
                    {decision.rationale ? ` · ${decision.rationale}` : ''}
                  </p>
                </li>
              ))}
            </ul>
          </section>

          <section className="panel" data-testid="evaluations">
            <h2>Evaluations</h2>
            {inspection.evaluations?.length ? (
              inspection.evaluations.map((evaluation) => (
                <div key={evaluation.id}>
                  <p>
                    {evaluation.evaluator_id}@{evaluation.evaluator_version} · rubric{' '}
                    {evaluation.rubric_version} · {evaluation.passed ? 'passed' : 'failed'}
                  </p>
                  <Payload label="score sheet" value={evaluation.scores} open={expanded} />
                </div>
              ))
            ) : (
              <p>Nothing was scored here.</p>
            )}
          </section>

          <section className="panel" data-testid="outputs">
            <h2>Outputs</h2>
            {inspection.outputs?.length ? (
              inspection.outputs.map((artefact) => (
                <div key={artefact.snapshot_id}>
                  <p className="muted">
                    {artefact.artifact_type} · {artefact.role} · {artefact.content_hash}
                  </p>
                  <Payload label={artefact.snapshot_id} value={artefact.content} open={expanded} />
                </div>
              ))
            ) : (
              <p>Nothing was produced.</p>
            )}
          </section>

          <section className="panel" data-testid="events">
            <h2>Timeline</h2>
            <ol className="timeline">
              {(inspection.events ?? []).map((event) => (
                <li key={event.id}>
                  <Disclosure
                    summary={`${event.sequence}. ${event.event_type} · ${event.actor_id}`}
                    open={expanded}
                  >
                    <p className="muted">
                      {event.timestamp} · {event.actor_type}
                      {event.causation_id ? ` · caused by ${event.causation_id}` : ''}
                    </p>
                    <Payload label="payload" value={event.payload} open={expanded} />
                  </Disclosure>
                </li>
              ))}
            </ol>
          </section>

          <section className="panel" data-testid="cost">
            <h2>Cost</h2>
            <p>
              {inspection.usage.model_calls} calls · {inspection.usage.input_tokens} in ·{' '}
              {inspection.usage.output_tokens} out ·{' '}
              {inspection.usage.cost_usd === null || inspection.usage.cost_usd === undefined
                ? 'cost not reported'
                : `$${inspection.usage.cost_usd}`}
            </p>
          </section>

          <section className="panel" data-testid="interventions">
            <h2>People</h2>
            <ul>
              {(inspection.interventions ?? []).map((intervention) => (
                <li key={intervention.id}>
                  {intervention.intervention_type.replace(/_/g, ' ')} by {intervention.user_id} ·{' '}
                  {intervention.occurred_at}
                </li>
              ))}
            </ul>
          </section>
        </section>
      )}
    </Loaded>
  );
}
