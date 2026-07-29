/**
 * The only screen served to someone who is not signed in (phase 13's slice).
 *
 * One field, because there is one credential: this is a personal tool with a
 * shared password, not an identity system. What it must not do is more
 * interesting than what it does — the password goes into a request and nowhere
 * else. Not the URL, not local storage, not a value the page keeps after the
 * request returns.
 */
import { useState, type FormEvent } from 'react';

import { ApiError, signIn } from '@/api/client';

export interface SignInScreenProps {
  onSignedIn: () => void;
}

export function SignInScreen({ onSignedIn }: SignInScreenProps) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      await signIn(password);
      // Dropped the moment it has been exchanged. A form that kept it would
      // keep it for as long as the tab is open.
      setPassword('');
      onSignedIn();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="screen screen--signin">
      <h1>groundscribe</h1>
      <p className="muted">This installation is locked. The password is in its `.env`.</p>

      <form className="signin" onSubmit={(event) => void submit(event)}>
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoFocus
        />
        <button type="submit" disabled={busy}>
          {busy ? 'signing in…' : 'sign in'}
        </button>
        {problem ? (
          <p role="alert" className="failure">
            {problem}
          </p>
        ) : null}
      </form>

      <p className="muted">
        The password crosses the network in the clear: this is HTTP on a local
        network, so treat the network as the boundary.
      </p>
    </section>
  );
}
