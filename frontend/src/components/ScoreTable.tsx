/**
 * How the score moved (phase 11).
 *
 * plan/11 → *Review history: score progression table*. Every row carries the
 * rubric version it was scored under and, where it failed, what it failed on:
 * phase 08's mitigation for false precision is that a score is never shown
 * without the reasoning that produced it, and a table of bare numbers would undo
 * that on the screen where it matters most.
 */
import type { ScoreView } from '@/api/client';

export interface ScoreTableProps {
  scores: readonly ScoreView[];
}

export function ScoreTable({ scores }: ScoreTableProps) {
  if (scores.length === 0) {
    return <p className="scores scores--empty">Not scored yet.</p>;
  }

  return (
    <table className="scores">
      <thead>
        <tr>
          <th scope="col">pass</th>
          <th scope="col">overall</th>
          <th scope="col">dimensions</th>
          <th scope="col">why</th>
          <th scope="col">rubric</th>
        </tr>
      </thead>
      <tbody>
        {scores.map((score, index) => (
          <tr key={score.execution_id + index} data-passed={String(score.passed)}>
            <td>{score.passed ? 'passed' : 'failed'}</td>
            <td>{score.overall}</td>
            <td>
              <ul className="scores__dimensions">
                {Object.entries(score.dimensions ?? {}).map(([name, value]) => (
                  <li key={name}>
                    {name}: {value}
                  </li>
                ))}
              </ul>
            </td>
            <td>
              <ul className="scores__failures">
                {(score.failures ?? []).map((failure, position) => (
                  <li key={position}>{String((failure as { detail?: unknown }).detail ?? '')}</li>
                ))}
              </ul>
            </td>
            <td>
              {score.rubric_version} · {score.evaluator_version}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
