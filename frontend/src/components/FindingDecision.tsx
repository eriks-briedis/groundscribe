/**
 * Deciding one review finding (plan/07 §8).
 *
 * The step between reviewing and planning, and the one the pipeline cannot take
 * for itself: a finding reaches a revision plan only once a person has accepted
 * it. Everything arrives `proposed`, so a review nobody has been through yields
 * an empty plan — and an empty plan passes every check downstream, which is how
 * a revision loop can run green and hand back the article unchanged.
 *
 * Three decisions, because they are the three a person makes. The other two
 * statuses are not choices: `proposed` is where a finding starts and
 * `suppressed` is the system holding one back.
 *
 * The endpoint comes from `decide_command`, which the backend supplies and
 * withholds once the finding is decided — the ledger keeps a decision on the
 * record rather than letting it be taken back, so a control offered again would
 * be offering something the backend refuses.
 */
import { useState } from 'react';

import { sendCommand, type ActionLink } from '@/api/client';

export interface FindingDecisionProps {
  command: ActionLink;
  actor: string;
  /** What the reviewer proposed, offered as the starting point for an edit. */
  suggestedCorrection?: string;
  onDecided: () => void;
}

type Pending = 'accept' | 'reject' | 'edit' | null;

export function FindingDecision({
  command,
  actor,
  suggestedCorrection = '',
  onDecided,
}: FindingDecisionProps) {
  const [busy, setBusy] = useState<Pending>(null);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const [correction, setCorrection] = useState(suggestedCorrection);
  const [reason, setReason] = useState('');

  if (!command.path) return null;
  const path = command.path;

  async function send(kind: Exclude<Pending, null>, body: Record<string, unknown>) {
    setBusy(kind);
    setError('');
    try {
      await sendCommand(path, { actor_id: actor, ...body });
      onDecided();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="decision">
      <div className="decision__actions">
        <button
          type="button"
          className="button--primary"
          disabled={busy !== null}
          onClick={() => void send('accept', { decision: 'accepted' })}
        >
          {busy === 'accept' ? 'Accepting…' : 'Accept'}
        </button>
        <button type="button" disabled={busy !== null} onClick={() => setEditing((on) => !on)}>
          {editing ? 'Cancel edit' : 'Accept with changes'}
        </button>
      </div>

      {editing ? (
        <div className="decision__edit">
          <label>
            What the rewrite should do instead
            <textarea
              value={correction}
              rows={3}
              onChange={(event) => setCorrection(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="button--primary"
            disabled={busy !== null || !correction.trim()}
            onClick={() =>
              void send('edit', {
                decision: 'edited',
                recommended_correction: correction,
                reason,
              })
            }
          >
            {busy === 'edit' ? 'Saving…' : 'Save and accept'}
          </button>
        </div>
      ) : null}

      <div className="decision__reject">
        <label>
          Why this does not apply
          <input
            type="text"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="required to reject"
          />
        </label>
        <button
          type="button"
          disabled={busy !== null || !reason.trim()}
          onClick={() => void send('reject', { decision: 'rejected', reason })}
          title={reason.trim() ? undefined : 'A rejection needs a reason'}
        >
          {busy === 'reject' ? 'Rejecting…' : 'Reject'}
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
