/**
 * The project screens (phase 11).
 *
 * plan/11 → *Project dashboard*, *Source workspace*, *Question queue*,
 * *Architecture board*, and the property that runs under all of them:
 * *primary views render artefacts (not a chat transcript)*.
 *
 * Each test asks the same two questions. Does the screen show the artefact a
 * person came to read — the claims, the questions, the concepts — and does it
 * show them as the backend reported, rather than as the UI decided to summarise
 * them?
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ArchitectureBoardScreen } from './ArchitectureBoardScreen';
import { DashboardScreen } from './DashboardScreen';
import { QuestionQueueScreen } from './QuestionQueueScreen';
import { SourceWorkspaceScreen } from './SourceWorkspaceScreen';
import { fakeBackend, fakeEventSource } from '@/test/backend';
import { architecture, dashboard, PROJECT_ID, questionQueue, sourceWorkspace } from '@/test/fixtures';

describe('the project dashboard', () => {
  it('opens on the state of the work, not on a conversation', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });

    render(<DashboardScreen projectId={PROJECT_ID} />);

    expect(await screen.findByRole('heading', { name: /read-through caching/i })).toBeInTheDocument();
    expect(screen.getByTestId('run-state')).toHaveTextContent('human approval required');
    expect(screen.queryByRole('log')).not.toBeInTheDocument();
  });

  it('reports the source, the questions and what the run has cost', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });

    render(<DashboardScreen projectId={PROJECT_ID} />);

    expect(await screen.findByTestId('source-completeness')).toHaveTextContent('7 claims');
    expect(screen.getByText(/what was the cold-cache p99\?/i)).toBeInTheDocument();
    expect(screen.getByTestId('usage')).toHaveTextContent('9 calls');
    expect(screen.getByTestId('usage')).toHaveTextContent('$0.108');
  });

  it('shows the failure the run has already had', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });

    render(<DashboardScreen projectId={PROJECT_ID} />);

    expect(await screen.findByText(/the provider timed out after 30s/i)).toBeInTheDocument();
  });

  it('follows a running job as the backend streams it', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });
    const stream = fakeEventSource();

    const view = render(<DashboardScreen projectId={PROJECT_ID} />);
    await screen.findByTestId('run-state');

    expect(stream.url()).toContain('/jobs/job-1/events');
    stream.emit({ event: 'job.progress', data: { stage: 'score_article', detail: 'scoring pass 1' } });
    expect(await screen.findByText(/scoring pass 1/i)).toBeInTheDocument();

    stream.emit({ event: 'job.status', data: { status: 'succeeded' } });
    expect(await screen.findByText(/succeeded/i)).toBeInTheDocument();

    view.unmount();
    expect(stream.closed()).toBe(true);
  });

  it('renders the actions the backend offered and nothing else', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });

    render(<DashboardScreen projectId={PROJECT_ID} />);
    await screen.findByTestId('run-state');

    const buttons = screen.getAllByRole('button').map((button) => button.textContent);
    expect(buttons).toContain('cancel');
    expect(buttons).not.toContain('approve final'); // offered, but not from here
  });

  it('says what went wrong instead of rendering an empty page', async () => {
    fakeBackend({});

    render(<DashboardScreen projectId={PROJECT_ID} />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/no route for/i);
  });
});

describe('the source workspace', () => {
  it('shows each claim with the segments it rests on', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/source-workspace`]: sourceWorkspace });

    render(<SourceWorkspaceScreen projectId={PROJECT_ID} />);

    const claim = await screen.findByTestId('claim-c1');
    expect(claim).toHaveTextContent('p99 latency fell to 120ms on warm cache.');
    expect(claim).toHaveTextContent('measured');
    await userEvent.click(screen.getByRole('button', { name: /evidence/i }));
    expect(screen.getByText(/p99 fell to 120ms on warm cache\./)).toBeInTheDocument();
  });

  it('marks confidential material and says which providers may see it', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/source-workspace`]: sourceWorkspace });

    render(<SourceWorkspaceScreen projectId={PROJECT_ID} />);

    expect(await screen.findByTestId('document-d1')).toHaveTextContent(/confidential/i);
    expect(screen.getByTestId('visibility')).toHaveTextContent('ollama');
    expect(screen.getByTestId('visibility')).toHaveTextContent('Project Halide');
  });

  it('links the structured source back to the execution that built it', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/source-workspace`]: sourceWorkspace });

    render(<SourceWorkspaceScreen projectId={PROJECT_ID} />);

    expect(await screen.findByRole('link', { name: /e2/ })).toHaveAttribute(
      'href',
      '#/executions/e2',
    );
  });
});

describe('the question queue', () => {
  it('leads with what blocks the run, and why each question matters', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: questionQueue });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    const items = await screen.findAllByRole('article');
    expect(items[0]).toHaveTextContent('What was the cold-cache p99?');
    expect(items[0]).toHaveTextContent('blocking');
    expect(items[0]).toHaveTextContent('The headline number is meaningless without it.');
  });

  it('keeps an answered question, with the answer that settled it', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: questionQueue });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    const answered = await screen.findByTestId('question-g2');
    expect(answered).toHaveTextContent('The colour parser only.');
    expect(answered).toHaveTextContent('ada');
  });

  it('answers where the backend said to answer, and says who answered', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/questions`]: questionQueue,
      [`/projects/${PROJECT_ID}/source-gaps/g1/answer`]: {
        project_id: PROJECT_ID,
        run_id: 'r1',
        state: 'source_model_extracting',
        available_actions: [],
      },
    });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);
    await userEvent.type(await screen.findByLabelText(/your answer/i), 'Cold-cache p99 was 640ms.');
    await userEvent.click(screen.getByRole('button', { name: /^answer$/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      path: `/projects/${PROJECT_ID}/source-gaps/g1/answer`,
      body: { text: 'Cold-cache p99 was 640ms.', answered_by: 'ada', response: 'answered' },
    });
  });

  it('offers the two honest non-answers the source model needs', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: questionQueue });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    const options = await screen.findByLabelText(/how you are answering/i);
    expect(options).toHaveTextContent(/unknown/i);
    expect(options).toHaveTextContent(/confidential/i);
  });
});

describe('the architecture board', () => {
  it('shows each concept as a card with the thesis it argues', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/architecture`]: architecture });

    render(<ArchitectureBoardScreen projectId={PROJECT_ID} actor="ada" />);

    const card = await screen.findByTestId('concept-a1');
    expect(card).toHaveTextContent('Read-through caching');
    expect(card).toHaveTextContent('Caching bought the latency.');
    expect(screen.getByTestId('concept-a2')).toHaveTextContent('Invalidation is the hard half.');
  });

  it('says which version is in force and who locked it', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/architecture`]: architecture });

    render(<ArchitectureBoardScreen projectId={PROJECT_ID} actor="ada" />);

    expect(await screen.findByTestId('current-version')).toHaveTextContent('arch-2');
    expect(screen.getByTestId('current-version')).toHaveTextContent(/locked by ada/i);
  });

  it('offers the seven edits the backend named, and no others', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/architecture`]: architecture });

    render(<ArchitectureBoardScreen projectId={PROJECT_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /edit the architecture/i }));

    const options = [...(await screen.findByLabelText(/operation/i)).querySelectorAll('option')];
    expect(options.map((option) => option.value)).toEqual(architecture.operations);
  });

  it('submits an edit where the backend said, naming who made it', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/architecture`]: architecture,
      [`/projects/${PROJECT_ID}/architecture/arch-2`]: {
        project_id: PROJECT_ID,
        run_id: 'r1',
        state: 'architecture_review_required',
        available_actions: [],
      },
    });

    render(<ArchitectureBoardScreen projectId={PROJECT_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /edit the architecture/i }));
    await userEvent.selectOptions(screen.getByLabelText(/operation/i), 'rename');
    await userEvent.click(screen.getByRole('checkbox', { name: /read-through caching/i }));
    await userEvent.type(screen.getByLabelText(/new title/i), 'Caching, honestly');
    await userEvent.click(screen.getByRole('button', { name: /^submit the edit$/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      method: 'PUT',
      path: `/projects/${PROJECT_ID}/architecture/arch-2`,
      body: {
        commands: [{ operation: 'rename', article_ids: ['a1'], title: 'Caching, honestly' }],
        requested_by: 'ada',
      },
    });
  });

  it('approves where the backend said, attributing the person', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/architecture`]: architecture,
      [`/projects/${PROJECT_ID}/architecture/arch-2/approve`]: {
        project_id: PROJECT_ID,
        run_id: 'r1',
        state: 'architecture_approved',
        available_actions: [],
      },
    });

    render(<ArchitectureBoardScreen projectId={PROJECT_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /approve architecture/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      path: `/projects/${PROJECT_ID}/architecture/arch-2/approve`,
      body: { actor_id: 'ada' },
    });
  });

  it('lets an earlier version be compared rather than only listed', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/architecture`]: architecture });

    render(<ArchitectureBoardScreen projectId={PROJECT_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /compare versions/i }));

    expect(screen.getByTestId('version-arch-1')).toHaveTextContent('One article about the cache.');
    expect(screen.getByTestId('version-arch-2')).toHaveTextContent('one about invalidation');
  });
});
