/**
 * The shell (phase 11).
 *
 * plan/11 → *separate editorial vs debugging modes*, and the artefact-first
 * navigation that holds the screens together: a person moves between a project,
 * an article and an execution, and the URL is what they are looking at.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { App } from './App';
import { fakeBackend } from '@/test/backend';
import { ARTICLE_ID, articleWorkspace, dashboard, PROJECT_ID } from '@/test/fixtures';

/** The shell is behind the sign-in screen, so every test here has a session. */
const SIGNED_IN = { '/auth/session': { authenticated: true } };

function go(hash: string) {
  window.location.hash = hash;
}

beforeEach(() => go(''));

describe('the shell', () => {
  it('shows the screen the address names', async () => {
    fakeBackend({
      ...SIGNED_IN,
      [`/projects/${PROJECT_ID}/dashboard`]: dashboard,
      [`/articles/${ARTICLE_ID}/workspace`]: articleWorkspace,
    });
    go(`#/projects/${PROJECT_ID}`);

    render(<App />);

    expect(await screen.findByRole('heading', { name: /read-through caching/i })).toBeInTheDocument();

    go(`#/articles/${ARTICLE_ID}`);
    await waitFor(() => expect(screen.getByTestId('version-body')).toBeInTheDocument());
  });

  it('starts editorial, and opens the payloads when asked to debug', async () => {
    fakeBackend({ ...SIGNED_IN, [`/projects/${PROJECT_ID}/dashboard`]: dashboard });
    go(`#/projects/${PROJECT_ID}`);

    render(<App />);
    const toggle = await screen.findByRole('button', { name: /debugging/i });

    expect(screen.getByTestId('mode')).toHaveTextContent(/editorial/i);
    await userEvent.click(toggle);
    expect(screen.getByTestId('mode')).toHaveTextContent(/debugging/i);
  });

  it('says so when the address means nothing', async () => {
    fakeBackend({ ...SIGNED_IN });
    go('#/nowhere');

    render(<App />);

    expect(await screen.findByText(/nothing here/i)).toBeInTheDocument();
  });
});
