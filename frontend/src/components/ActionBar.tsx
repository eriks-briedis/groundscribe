/**
 * What may be done here (phase 11).
 *
 * The bar has no opinion. It is handed the backend's `action_links` — the
 * offered actions, each with the request that takes it — and renders one button
 * per action that can actually be taken. It does not order them, filter them by
 * state, decide which is primary, or know what any of them means.
 *
 * That is the whole design. plan/11 forbids the frontend from holding transition
 * or routing rules, and the only way to be sure it holds none is for there to be
 * nowhere in this file that a state could be consulted.
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

/** `approve_final` → `approve final`, so a button reads as a sentence. */
function label(action: string): string {
  return action.replace(/_/g, ' ');
}

export function ActionBar({ links, actor, onCommand }: ActionBarProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  if (links.length === 0) {
    return <p className="actions actions--empty">Nothing to do here — the run is finished.</p>;
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
              <button type="button" disabled={busy !== null} onClick={() => void take(link)}>
                {busy === link.action ? `${label(link.action)}…` : label(link.action)}
              </button>
            ) : (
              // Offered by the machine, taken by the machine. Shown because the
              // backend offered it, and not as a button because nothing here can
              // take it — hiding it would misreport what the run may do.
              <span className="actions__pipeline" title="taken by the pipeline, not by you">
                {label(link.action)}
              </span>
            )}
          </li>
        ))}
      </ul>
      {problem ? (
        <p role="alert" className="actions__problem">
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
      <button type="button" disabled={busy} onClick={() => void start()}>
        {busy ? 'starting…' : `start ${label(command.action)}`}
      </button>
      {problem ? (
        <p role="alert" className="pending__problem">
          {problem}
        </p>
      ) : null}
    </div>
  );
}
