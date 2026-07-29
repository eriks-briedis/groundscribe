/**
 * The action bar (phase 11).
 *
 * plan/11 → *action buttons render exactly the backend's `available_actions`;
 * the UI never invents an action*, and *no client-side transition rules*.
 *
 * So the tests are mostly about restraint: what the bar shows when the backend
 * offers something it cannot perform, and what it does *not* show when the
 * backend offers nothing. A bar that guessed would pass a test that only checked
 * the happy case.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ActionBar } from './ActionBar';
import { fakeBackend } from '@/test/backend';
import { articleWorkspace } from '@/test/fixtures';

const links = articleWorkspace.action_links ?? [];

describe('the action bar', () => {
  it('offers a button for every action the backend says can be taken', () => {
    fakeBackend({});

    render(<ActionBar links={links} actor="ada" />);

    const offered = links.filter((link) => link.path).map((link) => link.action);
    for (const action of offered) {
      expect(screen.getByRole('button', { name: new RegExp(action.replace(/_/g, ' '), 'i') })).toBeInTheDocument();
    }
    expect(screen.getAllByRole('button')).toHaveLength(offered.length);
  });

  it('shows an action the API cannot perform without offering to perform it', () => {
    fakeBackend({});
    const withPipelineEdge = [...links, { action: 'fail', method: null, path: null, requires_actor: false }];

    render(<ActionBar links={withPipelineEdge} actor="ada" />);

    expect(screen.getByText(/fail/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /fail/i })).not.toBeInTheDocument();
  });

  it('posts exactly where the backend said, and attributes the person', async () => {
    const backend = fakeBackend({
      '/articles/a1/approve': { project_id: 'p1', run_id: 'r1', state: 'completed', available_actions: [] },
    });

    render(<ActionBar links={links} actor="ada" />);
    await userEvent.click(screen.getByRole('button', { name: /approve final/i }));

    expect(backend.commands).toHaveLength(1);
    expect(backend.commands[0]).toMatchObject({
      method: 'POST',
      path: '/articles/a1/approve',
      body: { actor_id: 'ada' },
    });
  });

  it('reports a refusal in the words the backend used', async () => {
    fakeBackend({});

    render(<ActionBar links={links} actor="ada" />);
    await userEvent.click(screen.getByRole('button', { name: /approve final/i }));

    // Nothing was routed, so the request 404s; the point is that the bar shows
    // what happened rather than silently doing nothing.
    expect(await screen.findByRole('alert')).toHaveTextContent(/no route for/i);
  });

  it('says so when the backend offers nothing at all', () => {
    fakeBackend({});

    render(<ActionBar links={[]} actor="ada" />);

    expect(screen.getByText(/nothing to do here/i)).toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
