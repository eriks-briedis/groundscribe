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

    expect(await screen.findByTestId('stat-claims')).toHaveTextContent('7');
    expect(screen.getByText(/what was the cold-cache p99\?/i)).toBeInTheDocument();
    expect(screen.getByTestId('stat-calls')).toHaveTextContent('9');
    expect(screen.getByTestId('stat-cost')).toHaveTextContent('$0.108');
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
    expect(buttons).toContain('Cancel');
    expect(buttons).not.toContain('Approve final'); // offered, but not from here
  });

  it('survives a backend that has not been restarted into the new payload', async () => {
    // What a person actually hits: the frontend reloads on save, the API process
    // does not, and one missing field used to take the whole screen down with a
    // stack trace instead of showing the artefacts it had.
    const stale: Record<string, unknown> = { ...dashboard };
    delete stale.journey;
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: stale });

    render(<DashboardScreen projectId={PROJECT_ID} />);

    expect(await screen.findByTestId('run-state')).toBeInTheDocument();
    expect(screen.queryByTestId('journey')).not.toBeInTheDocument();
    expect(screen.getByTestId('now')).toHaveTextContent(/older build/i);
    expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument();
  });

  it('offers the way out of a run whose job failed under it', async () => {
    // The situation that has no other exit: the state's own edges belong to the
    // pipeline, and the pipeline's job is the thing that failed. The backend
    // sends the command only when the run is actually stuck, so the screen shows
    // it whenever it arrives and never decides for itself.
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/dashboard`]: {
        ...dashboard,
        retry_command: {
          action: 'retry_failed_job',
          method: 'POST',
          path: `/projects/${PROJECT_ID}/retry`,
          requires_actor: true,
          taken_by: 'you',
        },
      },
      [`/projects/${PROJECT_ID}/retry`]: {
        project_id: PROJECT_ID,
        run_id: 'r1',
        state: 'source_model_extracting',
        available_actions: [],
      },
    });

    render(<DashboardScreen projectId={PROJECT_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /run that step again/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      path: `/projects/${PROJECT_ID}/retry`,
      body: { actor_id: 'ada' },
    });
  });

  it('says nothing about retrying a run that is not stuck', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });

    render(<DashboardScreen projectId={PROJECT_ID} />);

    await screen.findByTestId('run-state');
    expect(screen.queryByTestId('stranded')).not.toBeInTheDocument();
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
    expect(items[0]).toHaveTextContent(/blocks the run/i);
    expect(items[0]).toHaveTextContent('The headline number is meaningless without it.');
  });

  it('collects a round rather than rebuilding on every answer', async () => {
    // The queue is an interview: several answers, then one hand-back. The count
    // is what tells the author how much of the round they have done, and the
    // note is what tells them which button costs a model call.
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: questionQueue });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    const round = await screen.findByTestId('round');
    expect(round).toHaveTextContent(/1 answered/i);
    // One, not two. The third gap was found and not surfaced, and the run is
    // not waiting on it — counting it here would present the cap on how many
    // questions are asked as though it had never applied.
    expect(round).toHaveTextContent(/1 still open/i);
    expect(screen.getByRole('button', { name: /rebuild with these answers/i })).toBeEnabled();
  });

  it('separates what it is asking from what it merely noticed', async () => {
    // Extraction finds more gaps than it asks about — an author faced with
    // fifteen questions answers none — so the ones it held back are listed
    // apart from the ones the run is parked on.
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: questionQueue });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    const held = await screen.findByTestId('unasked');
    expect(held).toHaveTextContent(/which cache size was measured/i);
    expect(held).toHaveTextContent(/nothing is waiting on it/i);
  });

  it('hands the round back where the backend said, and says who did', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/questions`]: questionQueue,
      [`/projects/${PROJECT_ID}/source-questions/submit`]: {
        project_id: PROJECT_ID,
        run_id: 'r1',
        state: 'source_model_extracting',
        available_actions: [],
      },
    });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);
    await userEvent.click(
      await screen.findByRole('button', { name: /rebuild with these answers/i }),
    );

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      path: `/projects/${PROJECT_ID}/source-questions/submit`,
      body: { actor_id: 'ada' },
    });
  });

  it('offers no hand-back once the run has left the queue', async () => {
    fakeBackend({
      [`/projects/${PROJECT_ID}/questions`]: { ...questionQueue, submit: null },
    });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    await screen.findByTestId('question-g1');
    expect(screen.queryByTestId('round')).not.toBeInTheDocument();
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
    await userEvent.click(screen.getByRole('button', { name: /record answer/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      path: `/projects/${PROJECT_ID}/source-gaps/g1/answer`,
      body: { text: 'Cold-cache p99 was 640ms.', answered_by: 'ada', response: 'answered' },
    });
  });

  it('shows no form for a question the backend will not take an answer for', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: questionQueue });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    const closed = await screen.findByTestId('question-g3');
    expect(closed).toHaveTextContent('Which cache size was measured?');
    expect(closed).toHaveTextContent(/closed to answers/i);
    expect(closed.querySelector('form')).toBeNull();
  });

  it('offers the two honest non-answers the source model needs', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: questionQueue });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    const options = await screen.findByLabelText(/how you are answering/i);
    expect(options).toHaveTextContent(/don't know/i);
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
    expect(screen.getByTestId('concept-article-a2')).toHaveTextContent('Invalidation is the hard half.');
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

/**
 * Taking the trace out and destroying it (phase 16, found missing while auditing).
 *
 * Both endpoints shipped in phase 13 and neither was reachable, so the
 * local-first promise was true of the code and not of the product.
 */
describe('the trace panel', () => {
  it('warns before a full export, because the warning after one is too late', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });

    render(<DashboardScreen projectId={PROJECT_ID} />);

    const panel = await screen.findByTestId('privacy-panel');
    expect(panel).toHaveTextContent(/holds material marked confidential/i);
    expect(panel).toHaveTextContent(/refuses it until you say so explicitly/i);
  });

  it('asks for the sanitised export without an acknowledgement', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/dashboard`]: dashboard,
      [`/projects/${PROJECT_ID}/traces`]: {
        project_id: PROJECT_ID,
        sanitised: true,
        warnings: [],
        withheld_payloads: 4,
        runs: [{ id: 'r1' }],
      },
    });

    render(<DashboardScreen projectId={PROJECT_ID} />);
    await userEvent.click(await screen.findByRole('button', { name: /export, sanitised/i }));

    await waitFor(() => expect(screen.getByTestId('export-result')).toHaveTextContent('4 payload'));
    const asked = backend.requests.filter((r) => r.path === `/projects/${PROJECT_ID}/traces`);
    expect(asked[0]?.query.get('sanitise')).toBe('true');
    expect(asked[0]?.query.get('confidential_material_acknowledged')).toBe('false');
  });

  it('makes deleting a second, deliberate act, and says what stayed', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/dashboard`]: dashboard,
      [`/projects/${PROJECT_ID}/traces`]: {
        project_id: PROJECT_ID,
        payloads: 12,
        bytes_reclaimed: 4096,
        records_kept: 30,
        shared_payloads: 2,
      },
    });

    render(<DashboardScreen projectId={PROJECT_ID} />);
    await userEvent.click(await screen.findByRole('button', { name: /delete this project's traces/i }));

    // Nothing is sent by asking; the confirmation is where the act happens.
    expect(backend.commands).toHaveLength(0);
    expect(screen.getByTestId('confirm-delete')).toHaveTextContent(/the record that each call happened stays/i);

    await userEvent.click(screen.getByRole('button', { name: /^delete them$/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]?.method).toBe('DELETE');
    expect(await screen.findByTestId('delete-result')).toHaveTextContent('Kept 30 record');
  });
});

/**
 * A run carries its failures with it (found by using the thing).
 *
 * The panel is read when somebody wants to know why nothing is happening, and an
 * hour-old failure that was fixed and re-run reads exactly like the answer.
 */
describe('failures the run got past', () => {
  it('marks the ones a later run of the same stage answered', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });

    render(<DashboardScreen projectId={PROJECT_ID} actor="ada" />);
    const answered = await screen.findByText(/ran again since, and worked/i);

    expect(answered).toBeInTheDocument();
    expect(answered.closest('li')).toHaveAttribute('data-superseded', 'yes');
  });

  it('leaves a failure nothing has answered unmarked', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/dashboard`]: dashboard });

    render(<DashboardScreen projectId={PROJECT_ID} actor="ada" />);
    const current = await screen.findByText(/the provider timed out/i);

    expect(current.closest('li')).not.toHaveAttribute('data-superseded');
  });
});

/**
 * Changing an answer before the round is handed back (found by using the thing).
 *
 * An author works down a queue of questions and changes their mind halfway. The
 * screen used to replace the form with the recorded answer the moment it was
 * given, so there was one shot at each — and nothing had read it yet.
 */
describe('revising an answer', () => {
  const answered = {
    ...questionQueue,
    questions: [
      {
        ...(questionQueue.questions ?? [])[0]!,
        resolved: true,
        answer: {
          text: 'Cold cache p99 was 690ms.',
          question: (questionQueue.questions ?? [])[0]!.question,
          why_it_matters: '',
          response_type: 'answered',
          answered_by: 'ada',
          diff_snapshot_id: null as string | null,
        },
      },
    ],
  };

  it('offers to change an answer no rebuild has read', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: answered });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);

    expect(await screen.findByRole('button', { name: /change this answer/i })).toBeInTheDocument();
  });

  it('sends the new text to the same path', async () => {
    const backend = fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: answered });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /change this answer/i }));
    const box = screen.getByLabelText(/your answer/i);
    await userEvent.clear(box);
    await userEvent.type(box, 'Actually 720ms.');
    await userEvent.click(screen.getByRole('button', { name: /save this instead/i }));

    expect(backend.commands[0]).toMatchObject({
      path: answered.questions[0]!.answer_path,
      body: { text: 'Actually 720ms.', answered_by: 'ada' },
    });
  });

  it('offers nothing once the rebuild has read it', async () => {
    const consumed = structuredClone(answered);
    consumed.questions[0]!.answer!.diff_snapshot_id = 'snap-9';
    consumed.questions[0]!.answer_path = null;
    fakeBackend({ [`/projects/${PROJECT_ID}/questions`]: consumed });

    render(<QuestionQueueScreen projectId={PROJECT_ID} actor="ada" />);
    await screen.findByText(/rebuilt the source model/i);

    expect(screen.queryByRole('button', { name: /change this answer/i })).toBeNull();
  });
});
