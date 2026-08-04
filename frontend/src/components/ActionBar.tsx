/**
 * What may be done here (phase 11).
 *
 * The bar has no opinion. It is handed the backend's `action_links` — the
 * offered actions, each with the request that takes it and who it belongs to —
 * and renders one control per action. It does not order them, filter them by
 * state, decide which is primary, or know what any of them means.
 *
 * That is the whole design. plan/11 forbids the frontend from holding transition
 * or routing rules, and the only way to be sure it holds none is for there to be
 * nowhere in this file that a state could be consulted.
 *
 * The one thing it does read is `taken_by`, and only to stop lying: a link with
 * no path used to render as "taken by the pipeline", which is exactly wrong for
 * `answer_questions` — the author's own edge, taken on another screen, on a run
 * that is parked waiting for them to take it.
 */
import { useState } from 'react';

import { ApiError, sendCommand, type CommandResponse, type Schemas } from '@/api/client';

type ActionLink = Schemas['ActionLink'];

export interface ActionBarProps {
  links: readonly ActionLink[];
  /** Who is acting. Sent where the backend says attribution is required. */
  actor: string;
  onCommand?: (response: CommandResponse) => void;
}

/** `approve_final` → `Approve final`, so a button reads as an instruction. */
function label(action: string): string {
  const words = action.replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function ActionBar({ links, actor, onCommand }: ActionBarProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  if (links.length === 0) {
    return <p className="empty">Nothing to do here — the run is finished.</p>;
  }

  const take = async (link: ActionLink) => {
    if (!link.path) return;
    setBusy(link.action);
    setProblem(null);
    try {
      const response = await sendCommand(link.path, link.requires_actor ? { actor_id: actor } : {});
      onCommand?.(response);
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="actions">
      <ul className="actions__list">
        {links.map((link) => (
          <li key={link.action} className="actions__item">
            {link.path ? (
              <button
                type="button"
                className={link.taken_by === 'you' ? 'button--primary' : undefined}
                disabled={busy !== null}
                onClick={() => void take(link)}
              >
                {busy === link.action ? `${label(link.action)}…` : label(link.action)}
              </button>
            ) : link.taken_by === 'you' ? (
              // Yours to take, and not from here. Shown rather than hidden,
              // because a run parked on this edge is parked on *you*.
              <span className="actions__elsewhere">{label(link.action)} — on its own screen</span>
            ) : (
              // Offered by the machine, taken by the machine.
              <span className="actions__pipeline" title="taken by the pipeline, not by you">
                {label(link.action)}
              </span>
            )}
          </li>
        ))}
      </ul>
      {problem ? (
        <p role="alert" className="failure">
          {problem}
        </p>
      ) : null}
    </div>
  );
}

export interface StrandedProps {
  command: ActionLink | null | undefined;
  actor: string;
  onCommand?: (response: CommandResponse) => void;
}

/**
 * The way out of a run whose job failed under it.
 *
 * Its own control rather than another entry in the bar, because it is the only
 * thing on the screen that is *not* a transition: the run has not moved and will
 * not, and what is being offered is the work again. The backend sends it only
 * when the run is actually stuck, so its presence is the diagnosis — nothing
 * here decides when a run needs it.
 */
export function Stranded({ command, actor, onCommand }: StrandedProps) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!command?.path) return null;

  const again = async () => {
    setBusy(true);
    setProblem(null);
    try {
      onCommand?.(
        await sendCommand(command.path as string, command.requires_actor ? { actor_id: actor } : {}),
      );
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stranded" data-testid="stranded">
      <p className="stranded__note">
        The last step failed, and nothing is queued behind it. This run cannot go any further on
        its own.
      </p>
      <button type="button" className="button--primary" disabled={busy} onClick={() => void again()}>
        {busy ? 'Queueing…' : 'Run that step again'}
      </button>
      {problem ? (
        <p role="alert" className="failure">
          {problem}
        </p>
      ) : null}
    </div>
  );
}

export interface PendingCommandProps {
  command: ActionLink | null | undefined;
  onCommand?: (response: CommandResponse) => void;
}

/**
 * The one command a waiting state needs, named by the backend.
 *
 * Separate from the bar because it is a different kind of thing: not a
 * transition a person chooses, but the job the run is already committed to and
 * nobody has started yet.
 */
export function PendingCommand({ command, onCommand }: PendingCommandProps) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!command?.path) return null;

  const start = async () => {
    setBusy(true);
    setProblem(null);
    try {
      onCommand?.(await sendCommand(command.path as string));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pending">
      <button type="button" className="button--primary" disabled={busy} onClick={() => void start()}>
        {busy ? 'Starting…' : `Start ${label(command.action).toLowerCase()}`}
      </button>
      {problem ? (
        <p role="alert" className="failure">
          {problem}
        </p>
      ) : null}
    </div>
  );
}
