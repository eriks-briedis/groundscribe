/**
 * Two executions, side by side (phase 11).
 *
 * plan/11 → *Run comparison — side-by-side config/prompt/context/response/
 * output/score/cost/latency/preference/edit-distance diffs*.
 *
 * The rows are the backend's comparison, marked same or different by the side
 * that has both artefacts in hand. Preference is absent because phase 12 owns
 * it; a column of blanks would read as "nobody preferred either", which is a
 * different claim from "we have not asked".
 */
import { fetchComparison, type ExecutionComparison } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';

export interface RunComparisonScreenProps {
  left: string;
  right: string;
}

export function RunComparisonScreen({ left, right }: RunComparisonScreenProps) {
  const resource = useResource<ExecutionComparison>(
    () => fetchComparison(left, right),
    [left, right],
  );

  return (
    <Loaded resource={resource}>
      {(comparison) => (
        <section className="screen screen--comparison">
          <h1>
            {comparison.left.id} vs {comparison.right.id}
          </h1>

          <table className="comparison">
            <thead>
              <tr>
                <th scope="col">field</th>
                <th scope="col">
                  <a href={`#/executions/${comparison.left.id}`}>{comparison.left.id}</a>
                </th>
                <th scope="col">
                  <a href={`#/executions/${comparison.right.id}`}>{comparison.right.id}</a>
                </th>
              </tr>
            </thead>
            <tbody>
              {(comparison.differences ?? []).map((row) => (
                <tr key={row.field} data-same={String(row.same)}>
                  <th scope="row">{row.field}</th>
                  <td>{row.left ?? '—'}</td>
                  <td>{row.right ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <p data-testid="edit-distance">
            {comparison.output_edit_distance === null ||
            comparison.output_edit_distance === undefined
              ? 'One side produced nothing, so the outputs cannot be compared.'
              : `${comparison.output_edit_distance} lines apart`}
          </p>
        </section>
      )}
    </Loaded>
  );
}
