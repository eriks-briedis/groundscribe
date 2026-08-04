/**
 * Which models this project runs against, and how to change it (phase 15).
 *
 * The situation this exists for is specific: a source too long for the local
 * model's context window, in a project whose neighbours are fine. Before this,
 * the only lever was the shipped routing file, and pulling it moved every
 * project — so the fix for one project's stuck run was a change to everybody's.
 *
 * Three things about the wording here are deliberate, because this is the
 * control most likely to be misread as doing more than it does.
 *
 * **"Default" is a row, not a profile.** The backend reports `selected: null`
 * for it and leaves it out of `available`, because it is what not choosing
 * means. The list shows it first and labels it as the default so that choosing
 * it reads as *going back*, which is what it is.
 *
 * **Choosing is not consenting.** A project must still name the provider in its
 * allowed_providers before any material moves (phase 13), and this control
 * cannot do that — different decision, made elsewhere. So the note says the two
 * are separate rather than letting one button imply both.
 *
 * **It applies forward.** Stages already recorded ran under the policy they ran
 * under, and each execution says which. Saying so here stops the obvious wrong
 * expectation: that switching re-runs anything.
 */
import { useState } from 'react';

import { sendCommand, type RoutingProfiles } from '@/api/client';

/** What the default row is called in the interface, having no name of its own. */
const DEFAULT_LABEL = 'Default';

export interface RoutingProfileProps {
  /** What the dashboard was told. This panel makes no read of its own. */
  profiles: RoutingProfiles;
  actor: string;
  /** Told when the choice changed, so the screen around this re-reads itself. */
  onChanged?: () => void;
}

export function RoutingProfilePanel({ profiles, actor, onChanged }: RoutingProfileProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const available = profiles.available ?? [];

  async function choose(profile: string | null) {
    // The path and the method are the backend's, taken from what it published
    // rather than assembled here: a client that built this URL would be holding
    // its own copy of the routing table, and the copy is what goes stale.
    const command = profiles.command;
    if (busy || !command?.path) return;
    setBusy(true);
    setError('');
    try {
      await sendCommand(
        command.path,
        { profile, actor_id: actor },
        command.method ?? 'PUT',
      );
      onChanged?.();
    } catch (failure) {
      // The two failures a person can actually cause are a profile whose file
      // was removed and a name the backend will not accept. Both come back with
      // a sentence saying which, so the sentence is what gets shown.
      setError(failure instanceof Error ? failure.message : 'could not change the profile');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" data-testid="routing-profile">
      <h2>Models</h2>
      <p className="muted">
        Running <strong data-testid="routing-selected">{profiles.selected ?? DEFAULT_LABEL}</strong>{' '}
        (policy v{profiles.policy_version}).
      </p>

      <ul className="cards cards--choices">
        <li className="card">
          <button
            type="button"
            className="choice"
            aria-pressed={profiles.selected === null}
            disabled={busy || profiles.selected === null}
            onClick={() => void choose(null)}
          >
            {DEFAULT_LABEL}
          </button>
          <p className="muted">What this installation ships. Chosen by not choosing.</p>
        </li>
        {available.map((profile) => (
          <li key={profile} className="card">
            <button
              type="button"
              className="choice"
              aria-pressed={profiles.selected === profile}
              disabled={busy || profiles.selected === profile}
              onClick={() => void choose(profile)}
            >
              {profile}
            </button>
          </li>
        ))}
      </ul>

      {error ? (
        <p className="error" role="alert">
          {error}
        </p>
      ) : null}

      <p className="muted">
        Applies to the next stage that runs; stages already recorded keep the policy they ran
        under. Choosing a profile does not permit its provider — whether this project&rsquo;s
        material may go there is a separate declaration it makes for itself.
      </p>
    </section>
  );
}
