/**
 * The execution timeline and its filters (phase 11).
 *
 * plan/11 → *Execution timeline — chronological expandable trace events* and
 * *Trace filters*: failed executions, schema repairs, fallback models, blocking
 * findings, user overrides, high-cost calls, low-confidence scores,
 * confidential-data warnings, repeated unresolved issues.
 *
 * Filtering is a request, not a local array operation. The backend defines what
 * each filter means — a schema repair is a typed retry, a confidential warning
 * is a redaction placeholder in a stored payload — and none of that is visible
 * in the rows already on screen. A client-side filter would quietly answer a
 * different question with the same name.
 */
import { useState } from 'react';

import { fetchTrace, type TraceFilter, type TraceView } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';

function money(value: number | null | undefined): string {
  return value === null || value === undefined ? 'cost not reported' : `$${value}`;
}

export interface ExecutionTimelineScreenProps {
  projectId: string;
}

export function ExecutionTimelineScreen({ projectId }: ExecutionTimelineScreenProps) {
  const [filters, setFilters] = useState<TraceFilter[]>([]);
  const resource = useResource<TraceView>(
    () => fetchTrace(projectId, filters),
    [projectId, filters.join(',')],
  );

  const toggle = (filter: TraceFilter) =>
    setFilters((current) =>
      current.includes(filter)
        ? current.filter((name) => name !== filter)
        : [...current, filter],
    );

  return (
    <Loaded resource={resource}>
      {(trace) => (
        <section className="screen screen--trace">
          <h1>Execution timeline</h1>

          <fieldset className="filters">
            <legend>Show only</legend>
            {(trace.filters_available ?? []).map((filter) => (
              <label key={filter}>
                <input
                  type="checkbox"
                  value={filter}
                  checked={filters.includes(filter)}
                  onChange={() => toggle(filter)}
                />
                {filter.replace(/_/g, ' ')}
              </label>
            ))}
          </fieldset>

          {(trace.executions ?? []).length === 0 ? (
            <p>No execution matches.</p>
          ) : (
            (trace.executions ?? []).map((execution) => (
              <article className="card" key={execution.id}>
                <h2>
                  <a href={`#/executions/${execution.id}`}>{execution.id}</a> · {execution.stage}
                </h2>
                <p className="muted">
                  {execution.status} · {execution.started_at} · {execution.events} events ·{' '}
                  {execution.invocations} model calls · {money(execution.usage.cost_usd)}
                </p>
                {execution.error_message ? <p className="warning">{execution.error_message}</p> : null}
                {(execution.matched_filters ?? []).length > 0 ? (
                  <p className="muted">
                    matched: {(execution.matched_filters ?? []).join(', ').replace(/_/g, ' ')}
                  </p>
                ) : null}
              </article>
            ))
          )}
        </section>
      )}
    </Loaded>
  );
}
