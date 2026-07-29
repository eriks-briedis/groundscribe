/**
 * Two executions, side by side (phase 11, extended phase 12).
 *
 * plan/11 → *Run comparison — side-by-side config/prompt/context/response/
 * output/score/cost/latency/preference/edit-distance diffs*.
 *
 * The rows are the backend's comparison, marked same or different by the side
 * that has both artefacts in hand.
 *
 * Phase 12 added the contract underneath them, because this is the screen its
 * risk section is about: two runs of one stage differing is a fact, and what
 * that difference *proves* is a question a reader answers from whatever they
 * happen to believe about reproducibility. The refusal is rendered as a refusal
 * — a seventh clause reading like the six above it would turn "we do not
 * promise a hosted model repeats itself" into another thing the system does.
 *
 * Human preference is recorded against an experiment's arms rather than against
 * a pair of executions, so it belongs to the experiment report; a column of
 * blanks here would read as "nobody preferred either", which is a different
 * claim from "we have not asked".
 */
import { fetchComparison, type ExecutionComparison } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { Disclosure } from '@/components/Disclosure';

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

          <section className="panel" data-testid="reproducibility">
            <h2>What repeating a stage guarantees</h2>
            <ul className="contract">
              {(comparison.reproducibility ?? []).map((guarantee) => (
                <li
                  key={guarantee.name}
                  data-testid={`guarantee-${guarantee.name}`}
                  data-promised={String(guarantee.promised)}
                >
                  <p>
                    <span className="tag">{guarantee.promised ? 'promised' : 'not promised'}</span>{' '}
                    {guarantee.title}
                  </p>
                  <Disclosure summary="what that means">
                    <p>{guarantee.detail}</p>
                  </Disclosure>
                </li>
              ))}
            </ul>
          </section>
        </section>
      )}
    </Loaded>
  );
}
