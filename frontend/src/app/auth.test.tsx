/**
 * Signing in (a slice of phase 13, in the app).
 *
 * The cookie is `HttpOnly`, which is the point and also the constraint: the page
 * cannot look at it, so it asks the backend whether there is a session and shows
 * the form when there is not.
 *
 * The interesting cases are the edges. A session that expires while the page is
 * open must not leave a screen full of stale artefacts and silent failures, and
 * the password must never end up somewhere it can be read back — a URL, a
 * stored value, a rendered field.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { App } from './App';
import { fakeBackend } from '@/test/backend';
import { dashboard, PROJECT_ID } from '@/test/fixtures';

beforeEach(() => {
  window.location.hash = `#/projects/${PROJECT_ID}`;
});

describe('the sign-in screen', () => {
  it('is what an unauthenticated person sees, whatever they asked for', async () => {
    fakeBackend({
      '/auth/session': { authenticated: false },
      [`/projects/${PROJECT_ID}/dashboard`]: dashboard,
    });

    render(<App />);

    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.queryByTestId('run-state')).not.toBeInTheDocument();
  });

  it('sends the password to the backend and then shows the work', async () => {
    let signedIn = false;
    const backend = fakeBackend({
      '/auth/session': () => ({ authenticated: signedIn }),
      '/auth/login': () => {
        signedIn = true;
        return {};
      },
      [`/projects/${PROJECT_ID}/dashboard`]: dashboard,
    });

    render(<App />);
    await userEvent.type(await screen.findByLabelText(/password/i), 'correct horse');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByTestId('run-state')).toBeInTheDocument();
    expect(backend.commands[0]).toMatchObject({
      path: '/auth/login',
      body: { password: 'correct horse' },
    });
  });

  it('says the password was wrong without saying anything else', async () => {
    fakeBackend({ '/auth/session': { authenticated: false } });

    render(<App />);
    await userEvent.type(await screen.findByLabelText(/password/i), 'hunter2');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    // No route for /auth/login here, so the attempt fails: the form must report
    // it and stay put rather than pretend it worked.
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
  });

  it('never puts the password anywhere it can be read back', async () => {
    fakeBackend({ '/auth/session': { authenticated: false } });

    render(<App />);
    const field = await screen.findByLabelText(/password/i);
    await userEvent.type(field, 'correct horse');

    expect(field).toHaveAttribute('type', 'password');
    expect(window.location.href).not.toContain('correct');
    expect(window.localStorage.length).toBe(0);
    expect(document.cookie).not.toContain('correct');
  });

  it('returns to the form when a session expires mid-session', async () => {
    let signedIn = true;
    const refused = () =>
      new Response(JSON.stringify({ detail: 'sign in first' }), {
        status: 401,
        headers: { 'content-type': 'application/json' },
      });
    fakeBackend({
      '/auth/session': () => ({ authenticated: signedIn }),
      [`/projects/${PROJECT_ID}/dashboard`]: () => (signedIn ? dashboard : refused()),
      [`/projects/${PROJECT_ID}/questions`]: () => (signedIn ? { questions: [] } : refused()),
    });

    render(<App />);
    await screen.findByTestId('run-state');

    // The cookie lapses, and the next screen's read is refused. Nothing told the
    // app in advance — which is the case worth testing.
    signedIn = false;
    window.location.hash = `#/projects/${PROJECT_ID}/questions`;

    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
  });

  it('offers a way out, which ends the session at the backend', async () => {
    const backend = fakeBackend({
      '/auth/session': { authenticated: true },
      '/auth/logout': {},
      [`/projects/${PROJECT_ID}/dashboard`]: dashboard,
    });

    render(<App />);
    await screen.findByTestId('run-state');
    await userEvent.click(screen.getByRole('button', { name: /sign out/i }));

    await waitFor(() => expect(backend.commands.map((c) => c.path)).toContain('/auth/logout'));
    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
  });
});
