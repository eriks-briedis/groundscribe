/**
 * Handing over a review's decisions, once (IMPROVEMENTS §10).
 *
 * Triage is the pipeline's slowest human step and it was priced per finding: a
 * request, a stage execution and a full workspace reload each. One run recorded
 * 34 of them, five seconds apart, for what the author was doing in one sitting.
 * Their words were *slow, difficult and clumsy*.
 *
 * The asymmetry that made it worse: of the ten findings on that run, **one**
 * changed the article. The second review's five were all `optional` and all
 * rejected, every one recording that the prior score's complaints no longer
 * held — five deliberate refusals, each needing its own typed reason, to say
 * that there was nothing to do. The work does not scale with the decisions that
 * matter; it scales with the findings returned.
 *
 * Hence the bulk control. It is deliberately **not** IMPROVEMENTS §10's advisor:
 * nothing here proposes a decision, and nothing arrives pre-filled. It lets a
 * person who has read five findings and reached one conclusion about all of them
 * say so once, which is what they were doing five times.
 *
 * The endpoint comes from `triage_command`, which the backend supplies and
 * withholds once nothing is undecided.
 */
import { useState } from 'react';

import { sendCommand, type ActionLink } from '@/api/client';
import type { Verdict } from '@/components/FindingDecision';

export interface TriageProps {
  command: ActionLink;
  actor: string;
  /** Every finding still waiting, by id — what the bulk control acts on. */
  undecidedIds: string[];
  verdicts: Record<string, Verdict>;
  onBulk: (verdicts: Record<string, Verdict>) => void;
  onSubmitted: () => void;
}

export function Triage({
  command,
  actor,
  undecidedIds,
  verdicts,
  onBulk,
  onSubmitted,
}: TriageProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [bulkReason, setBulkReason] = useState('');

  if (!command.path) return null;
  const path = command.path;

  const decided = undecidedIds.filter((id) => verdicts[id]);
  const remaining = undecidedIds.length - decided.length;

  async function submit() {
    setBusy(true);
    setError('');
    try {
      await sendCommand(path, {
        actor_id: actor,
        decisions: decided.map((finding_id) => ({ finding_id, ...verdicts[finding_id] })),
      });
      onSubmitted();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="triage" data-testid="triage">
      <div className="triage__bulk">
        <label>
          Reject the rest, for this reason
          <input
            type="text"
            value={bulkReason}
            disabled={busy}
            onChange={(event) => setBulkReason(event.target.value)}
            placeholder="required to reject"
          />
        </label>
        <button
          type="button"
          disabled={busy || !bulkReason.trim() || remaining === 0}
          onClick={() =>
            onBulk(
              Object.fromEntries(
                undecidedIds
                  .filter((id) => !verdicts[id])
                  .map((id) => [id, { decision: 'rejected', reason: bulkReason } as Verdict]),
              ),
            )
          }
          title={bulkReason.trim() ? undefined : 'A rejection needs a reason'}
        >
          Reject remaining {remaining}
        </button>
      </div>

      <div className="triage__submit">
        <p className="muted" data-testid="triage-count">
          {decided.length} of {undecidedIds.length} decided
          {remaining ? `, ${remaining} still open` : ''}
        </p>
        <button
          type="button"
          className="button--primary"
          disabled={busy || remaining > 0}
          onClick={() => void submit()}
          title={remaining ? 'Every finding needs a decision before the review is handed over' : undefined}
        >
          {busy ? 'Submitting…' : `Submit ${decided.length} decisions`}
        </button>
      </div>

      {error ? (
        <p role="alert" className="error">
          {error}
        </p>
      ) : null}
    </div>
  );
}
