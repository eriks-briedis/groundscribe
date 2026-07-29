/**
 * Putting source material into a project (phase 11, found missing while using it).
 *
 * The workspace showed what had been extracted and offered no way to put
 * anything in — a project opens on "0 documents · 0 segments · 0 claims" and
 * stops there, because ingestion is a command with no workflow action behind it
 * and the action bar therefore never mentions it.
 *
 * The three formats are the backend's vocabulary, and the distinction is real:
 * a heading in Markdown is a structural fact, while the same line in pasted
 * notes is just a line, and segmentation follows that. So the form asks rather
 * than guessing.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { SourceWorkspaceScreen } from './SourceWorkspaceScreen';
import { fakeBackend } from '@/test/backend';
import { PROJECT_ID, sourceWorkspace } from '@/test/fixtures';

const EMPTY = {
  ...sourceWorkspace,
  documents: [],
  claims: [],
  unknowns: [],
  source_model: null,
};

describe('adding source material', () => {
  it('is offered on a project that has none, instead of an empty page', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/source-workspace`]: EMPTY });

    render(<SourceWorkspaceScreen projectId={PROJECT_ID} />);

    expect(await screen.findByText(/no source material yet/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^text$/i)).toBeInTheDocument();
  });

  it('posts what was pasted, and says how it should be read', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/source-workspace`]: EMPTY,
      [`/projects/${PROJECT_ID}/sources`]: {
        project_id: PROJECT_ID,
        run_id: 'r1',
        state: 'source_ingested',
        available_actions: [],
      },
    });

    render(<SourceWorkspaceScreen projectId={PROJECT_ID} />);
    await userEvent.type(await screen.findByLabelText(/document title/i), 'Incident notes');
    await userEvent.type(screen.getByLabelText(/^text$/i), 'p99 fell to 120ms on warm cache.');
    await userEvent.selectOptions(screen.getByLabelText(/format/i), 'markdown');
    await userEvent.click(screen.getByRole('button', { name: /add this source/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      path: `/projects/${PROJECT_ID}/sources`,
      body: {
        title: 'Incident notes',
        text: 'p99 fell to 120ms on warm cache.',
        source_format: 'markdown',
        confidential: false,
      },
    });
  });

  it('can mark material that must never leave the machine', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/source-workspace`]: EMPTY,
      [`/projects/${PROJECT_ID}/sources`]: {
        project_id: PROJECT_ID,
        run_id: 'r1',
        state: 'source_ingested',
        available_actions: [],
      },
    });

    render(<SourceWorkspaceScreen projectId={PROJECT_ID} />);
    await userEvent.type(await screen.findByLabelText(/document title/i), 'Customer names');
    await userEvent.type(screen.getByLabelText(/^text$/i), 'Acme Ltd, Initech');
    await userEvent.click(screen.getByLabelText(/confidential/i));
    await userEvent.click(screen.getByRole('button', { name: /add this source/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]?.body).toMatchObject({ confidential: true });
  });

  it('takes a file, because that is what "a document" usually is', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/source-workspace`]: EMPTY });

    render(<SourceWorkspaceScreen projectId={PROJECT_ID} />);
    const file = new File(['# Notes\n\nThe cache went in on Tuesday.'], 'notes.md', {
      type: 'text/markdown',
    });
    await userEvent.upload(await screen.findByLabelText(/choose a file/i), file);

    // Read into the form rather than posted straight off: a person should see
    // what they are about to send, and title it.
    await waitFor(() =>
      expect(screen.getByLabelText(/^text$/i)).toHaveValue(
        '# Notes\n\nThe cache went in on Tuesday.',
      ),
    );
    expect(screen.getByLabelText(/document title/i)).toHaveValue('notes.md');
  });

  it('reports a refusal rather than appearing to have added something', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/source-workspace`]: EMPTY });

    render(<SourceWorkspaceScreen projectId={PROJECT_ID} />);
    await userEvent.type(await screen.findByLabelText(/document title/i), 'Notes');
    await userEvent.type(screen.getByLabelText(/^text$/i), 'something');
    await userEvent.click(screen.getByRole('button', { name: /add this source/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/no route for/i);
  });
});
