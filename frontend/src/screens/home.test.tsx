/**
 * The way in (phase 11, found missing while using it).
 *
 * Every other screen addresses a project by id. This is where the first id comes
 * from — and where a project that does not exist yet comes from, which is the
 * part the application was shipped without.
 *
 * The form asks for the bounds a project publishes under because the backend
 * demands them: audience, platform, depth, length, which providers may see the
 * source. That is not ceremony. A project created without them would be a
 * project whose brief has no contract to hold it to.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { HomeScreen } from './HomeScreen';
import { fakeBackend } from '@/test/backend';
import { PROJECT_ID } from '@/test/fixtures';

const INDEX = {
  projects: [
    {
      id: PROJECT_ID,
      title: 'Read-through caching',
      description: 'How the render pipeline got faster.',
      author_id: 'ada',
      run_id: 'r1',
      state: 'human_approval_required',
      articles: 2,
      opened_at: '2026-07-25T11:00:00Z',
    },
  ],
};

beforeEach(() => {
  window.location.hash = '';
});

describe('the way in', () => {
  it('lists what is here, with a way to open each', async () => {
    fakeBackend({ '/projects': INDEX });

    render(<HomeScreen actor="ada" />);

    const project = await screen.findByRole('link', { name: /read-through caching/i });
    expect(project).toHaveAttribute('href', `#/projects/${PROJECT_ID}`);
    expect(screen.getByTestId(`project-${PROJECT_ID}`)).toHaveTextContent('human approval required');
    expect(screen.getByTestId(`project-${PROJECT_ID}`)).toHaveTextContent('2 articles');
  });

  it('says the installation is empty rather than looking broken', async () => {
    fakeBackend({ '/projects': { projects: [] } });

    render(<HomeScreen actor="ada" />);

    expect(await screen.findByText(/nothing here yet/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/title/i)).toBeInTheDocument();
  });

  it('creates a project with the bounds it will publish under', async () => {
    const backend = fakeBackend({
      '/projects': ({ method }) =>
        method === 'POST'
          ? { project_id: 'new-1', run_id: 'r9', state: 'source_ingested', available_actions: [] }
          : INDEX,
    });

    render(<HomeScreen actor="ada" />);
    await userEvent.type(await screen.findByLabelText(/title/i), 'Invalidation');
    await userEvent.type(screen.getByLabelText(/audience/i), 'backend engineers');
    await userEvent.type(screen.getByLabelText(/platform/i), 'personal blog');
    await userEvent.click(screen.getByRole('button', { name: /start the project/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      path: '/projects',
      body: {
        title: 'Invalidation',
        author_id: 'ada',
        constraints: { audience: 'backend engineers', platform: 'personal blog' },
      },
    });
  });

  it('opens the project it just created, rather than leaving it to be found', async () => {
    fakeBackend({
      '/projects': ({ method }) =>
        method === 'POST'
          ? { project_id: 'new-1', run_id: 'r9', state: 'source_ingested', available_actions: [] }
          : INDEX,
    });

    render(<HomeScreen actor="ada" />);
    await userEvent.type(await screen.findByLabelText(/title/i), 'Invalidation');
    await userEvent.type(screen.getByLabelText(/audience/i), 'backend engineers');
    await userEvent.type(screen.getByLabelText(/platform/i), 'personal blog');
    await userEvent.click(screen.getByRole('button', { name: /start the project/i }));

    await waitFor(() => expect(window.location.hash).toBe('#/projects/new-1'));
  });

  it('reports a refusal instead of appearing to have worked', async () => {
    fakeBackend({ '/projects': ({ method }) => (method === 'POST' ? null : INDEX) });

    render(<HomeScreen actor="ada" />);
    await userEvent.type(await screen.findByLabelText(/title/i), 'Invalidation');
    await userEvent.click(screen.getByRole('button', { name: /start the project/i }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(window.location.hash).not.toContain('projects/');
  });
});
