/**
 * Deciding review findings (plan/07 §8), as one pass rather than one at a time.
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
 * **Why this holds state instead of sending it.** Each button used to POST and
 * then reload the whole workspace — brief, version, diff, findings, plan,
 * scores, lineage, approval. One run recorded 34 of those, five seconds apart,
 * for what the author was doing in a single sitting. So a decision is now
 * *recorded here* and handed over by whoever owns the list, once.
 *
 * The component still knows nothing about where anything is posted. The parent
 * is given the endpoint by the backend, exactly as this used to be.
 */
import { useState } from 'react';

import { sendCommand, type ActionLink } from '@/api/client';

export type Verdict =
  | { decision: 'accepted' }
  | { decision: 'rejected'; reason: string }
  | { decision: 'edited'; recommended_correction: string; reason: string };

export interface FindingDecisionProps {
  /** What the author has decided so far, or nothing. */
  verdict?: Verdict;
  /** What the reviewer proposed, offered as the starting point for an edit. */
  suggestedCorrection?: string;
  disabled?: boolean;
  onDecide: (verdict: Verdict | undefined) => void;
}

export function FindingDecision({
  verdict,
  suggestedCorrection = '',
  disabled = false,
  onDecide,
}: FindingDecisionProps) {
  const [editing, setEditing] = useState(false);
  const [correction, setCorrection] = useState(suggestedCorrection);
  const [reason, setReason] = useState('');

  // Undoing before submitting is the affordance a per-request version could not
  // have: the ledger keeps a decision rather than letting it be taken back, so
  // the only safe place to change one's mind is before the batch is handed over.
  const decided = verdict?.decision;

  return (
    <div className="decision" data-decided={decided ?? 'undecided'}>
      <div className="decision__actions">
        <button
          type="button"
          className={decided === 'accepted' ? 'button--primary' : undefined}
          aria-pressed={decided === 'accepted'}
          disabled={disabled}
          onClick={() =>
            onDecide(decided === 'accepted' ? undefined : { decision: 'accepted' })
          }
        >
          Accept
        </button>
        <button
          type="button"
          aria-pressed={decided === 'edited'}
          disabled={disabled}
          onClick={() => setEditing((on) => !on)}
        >
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
              disabled={disabled}
              onChange={(event) => setCorrection(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="button--primary"
            disabled={disabled || !correction.trim()}
            onClick={() => {
              onDecide({ decision: 'edited', recommended_correction: correction, reason });
              setEditing(false);
            }}
          >
            Save
          </button>
        </div>
      ) : null}

      <div className="decision__reject">
        <label>
          Why this does not apply
          <input
            type="text"
            value={reason}
            disabled={disabled}
            onChange={(event) => {
              setReason(event.target.value);
              // Keep a recorded rejection in step with the reason behind it. A
              // reason edited after the fact would otherwise be submitted with
              // the text the author has since replaced.
              if (decided === 'rejected') {
                onDecide({ decision: 'rejected', reason: event.target.value });
              }
            }}
            placeholder="required to reject"
          />
        </label>
        <button
          type="button"
          aria-pressed={decided === 'rejected'}
          disabled={disabled || !reason.trim()}
          onClick={() =>
            onDecide(decided === 'rejected' ? undefined : { decision: 'rejected', reason })
          }
          title={reason.trim() ? undefined : 'A rejection needs a reason'}
        >
          Reject
        </button>
      </div>
    </div>
  );
}

/**
 * One finding, decided and sent on its own.
 *
 * For the review history, where a person is reading rounds rather than clearing
 * a queue: they notice one finding and decide it. Batching would be the wrong
 * shape there — there is no pass to hand over — so this keeps the single-finding
 * endpoint, which the backend still serves through the same service method.
 *
 * The workspace does not use it. That screen is the queue, and the whole point
 * of the change is that working a queue is one submission.
 */
export interface DecideOneProps {
  command: ActionLink;
  actor: string;
  suggestedCorrection?: string;
  onDecided: () => void;
}

export function DecideOne({
  command,
  actor,
  suggestedCorrection = '',
  onDecided,
}: DecideOneProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  if (!command.path) return null;
  const path = command.path;

  async function send(verdict: Verdict | undefined) {
    if (!verdict) return;
    setBusy(true);
    setError('');
    try {
      await sendCommand(path, { actor_id: actor, ...verdict });
      onDecided();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <FindingDecision
        suggestedCorrection={suggestedCorrection}
        disabled={busy}
        onDecide={(verdict) => void send(verdict)}
      />
      {error ? (
        <p role="alert" className="error">
          {error}
        </p>
      ) : null}
    </>
  );
}
