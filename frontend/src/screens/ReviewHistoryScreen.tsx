/**
 * The review history (phase 11).
 *
 * plan/11 → *score progression table + issue history (resolved/reopened/new,
 * disagreements, stagnation warnings, rubric/reviewer versions, confidence)*.
 *
 * The lifecycle label on each finding is the backend's: it comes from phase 07's
 * fingerprints, which is what makes "the same finding again" answerable across
 * rounds where a reviewer renumbers its ids freely. Recomputing it here from the
 * text would give a different answer, confidently.
 */
import { fetchReviewHistory, type ReviewHistory } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { FindingDecision } from '@/components/FindingDecision';
import { ScoreTable } from '@/components/ScoreTable';

export interface ReviewHistoryScreenProps {
  articleId: string;
  actor: string;
}

export function ReviewHistoryScreen({ articleId, actor }: ReviewHistoryScreenProps) {
  const resource = useResource<ReviewHistory>(() => fetchReviewHistory(articleId), [articleId]);

  return (
    <Loaded resource={resource}>
      {(history) => (
        <section className="screen screen--reviews">
          <h1>Review history</h1>

          {(history.warnings ?? []).map((warning, index) => (
            <p role="status" className="warning" key={index}>
              {warning}
            </p>
          ))}

          <section className="panel">
            <h2>Scores</h2>
            <ScoreTable scores={history.scores ?? []} />
          </section>

          {(history.rounds ?? []).map((round) => (
            <article className="card" key={round.review_id}>
              <h2>
                v{round.version_ordinal} · round {round.round} · {round.verdict}
              </h2>
              {round.execution_id ? (
                <p className="muted">
                  <a href={`#/executions/${round.execution_id}`}>the review that said so</a>
                </p>
              ) : null}
              <ul className="findings">
                {(round.issues ?? []).map((issue) => (
                  <li key={issue.id} data-lifecycle={issue.lifecycle}>
                    <span className="tag">{issue.severity}</span>{' '}
                    <span className="tag">{issue.lifecycle}</span> {issue.description}
                    <p className="muted">
                      {issue.status} · confidence {issue.reviewer_confidence}
                      {issue.decision_reason ? ` · ${issue.decision_reason}` : ''}
                    </p>
                    {issue.recommended_correction ? (
                      <p className="finding__correction">{issue.recommended_correction}</p>
                    ) : null}
                    {/* Offered only while the backend still offers it: a decision
                        stays on the record rather than being taken back. */}
                    {issue.decide_command ? (
                      <FindingDecision
                        command={issue.decide_command}
                        actor={actor}
                        suggestedCorrection={issue.recommended_correction ?? ''}
                        onDecided={resource.reload}
                      />
                    ) : null}
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </section>
      )}
    </Loaded>
  );
}
