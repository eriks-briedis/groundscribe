/**
 * The article screens (phase 11).
 *
 * plan/11 → *Article workspace* (brief, current version, findings, plan, voice
 * rules, previous version, diff, scores, available actions, producing execution,
 * branch lineage), *Review history*, *Lineage graph*, and the human-approval
 * view that has to surface everything before a person publishes.
 *
 * The approval tests are the sharpest ones here. plan/11 lists what a person
 * must be shown before approving, and every item on that list is something the
 * interface could plausibly leave out to look tidier.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ArticleWorkspaceScreen } from './ArticleWorkspaceScreen';
import { ReviewHistoryScreen } from './ReviewHistoryScreen';
import { fakeBackend } from '@/test/backend';
import { ARTICLE_ID, articleWorkspace, reviewHistory } from '@/test/fixtures';

const routes = { [`/articles/${ARTICLE_ID}/workspace`]: articleWorkspace };

describe('the article workspace', () => {
  it('shows the version as prose, next to the brief it was written to', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    expect(await screen.findByTestId('version-body')).toHaveTextContent(
      'That number is why anyone would read this.',
    );
    expect(screen.getByTestId('brief')).toHaveTextContent(
      'Caching bought the latency, invalidation cost it back.',
    );
  });

  it('renders the article as the markdown it is, with the source a click away', async () => {
    fakeBackend({
      [`/articles/${ARTICLE_ID}/workspace`]: {
        ...articleWorkspace,
        current_version: {
          ...articleWorkspace.current_version,
          body: '## Why it was slow\n\nThe renderer *rebuilt* every fragment.',
        },
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    const body = await screen.findByTestId('version-body');
    expect(body.querySelector('h2')).toHaveTextContent('Why it was slow');
    expect(body.querySelector('em')).toHaveTextContent('rebuilt');

    await userEvent.click(screen.getByRole('button', { name: /markdown source/i }));
    expect(screen.getByText(/## Why it was slow/)).toBeInTheDocument();
  });

  it('shows the reviewer findings with the evidence behind each one', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    const finding = await screen.findByTestId('finding-i1');
    expect(finding).toHaveTextContent('The latency figure is stated without the cache condition.');
    expect(finding).toHaveTextContent('blocking');
    expect(finding).toHaveTextContent('The source marks it warm-cache only.');
  });

  it('shows the voice rules in force, and where each came from', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    const voice = await screen.findByTestId('voice');
    expect(voice).toHaveTextContent('Never use an em dash.');
    expect(voice).toHaveTextContent('hard rule');
    expect(voice).toHaveTextContent('ada@2 (global)');
  });

  it('shows the diff against the previous version, and the score progression', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    expect(await screen.findByTestId('diff-summary')).toHaveTextContent('+1');
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveTextContent('89.5');
  });

  it('carries the source evidence the article rests on', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    const evidence = await screen.findByTestId('source-evidence');
    expect(evidence).toHaveTextContent('p99 latency fell to 120ms on warm cache.');
    expect(evidence).toHaveTextContent('measured');
  });

  it('names the execution that produced what is on screen', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    expect(await screen.findByRole('link', { name: /align_voice/ })).toHaveAttribute(
      'href',
      '#/executions/e9',
    );
  });

  it('draws the lineage of the versions behind it', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    const graph = await screen.findByTestId('lineage');
    expect(graph.querySelectorAll('[data-node]')).toHaveLength(3);
    expect(graph.querySelectorAll('[data-edge]')).toHaveLength(2);
  });
});

describe('the approval view', () => {
  it('surfaces everything plan/11 requires before a person approves', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /ready to approve/i }));

    const approval = screen.getByTestId('approval');
    expect(approval).toHaveTextContent(/1 rewrite/i); // rewrite rounds
    expect(approval).toHaveTextContent('The latency figure is stated without the cache condition.');
    expect(approval).toHaveTextContent(/validation passed/i);
    expect(approval).toHaveTextContent('llama3.1:70b-instruct'); // model + prompt versions
    expect(approval).toHaveTextContent('generate_initial_draft');
    expect(approval).toHaveTextContent('$0.108'); // cost and usage
    expect(approval).toHaveTextContent('ada'); // the interventions so far
  });

  it('offers approval as the backend offers it, not as the screen decides', async () => {
    const backend = fakeBackend({
      ...routes,
      [`/articles/${ARTICLE_ID}/approve`]: {
        project_id: 'p1',
        run_id: 'r1',
        state: 'completed',
        available_actions: [],
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /approve final/i }));

    expect(backend.commands[0]).toMatchObject({
      path: `/articles/${ARTICLE_ID}/approve`,
      body: { actor_id: 'ada' },
    });
  });
});

describe('the review history', () => {
  it('shows each round, its verdict, and what each finding did', async () => {
    fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: reviewHistory });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} actor="ada" />);

    const rounds = await screen.findAllByRole('article');
    expect(rounds[0]).toHaveTextContent(/revise/i);
    expect(rounds[0]).toHaveTextContent(/new/i);
    expect(rounds[1]).toHaveTextContent(/polish/i);
    expect(rounds[1]).toHaveTextContent(/repeated/i);
  });

  it('shows the score progression beside the rounds that earned it', async () => {
    fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: reviewHistory });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} actor="ada" />);

    const rows = await screen.findAllByRole('row');
    expect(rows.slice(1)[0]).toHaveTextContent('82');
    expect(rows.slice(1)[1]).toHaveTextContent('89.5');
  });

  it('passes on the stagnation warning the backend raised', async () => {
    fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: reviewHistory });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} actor="ada" />);

    expect(await screen.findByRole('status')).toHaveTextContent(
      /two rounds have not moved the score/i,
    );
  });
});

/**
 * Running an article again (phase 16, found missing while trying to use it).
 *
 * `POST /executions/{id}/replay` shipped in phase 12 with nothing calling it, so
 * after fixing a voice profile there was no way to apply it to the article that
 * had exposed the problem — short of starting the project over.
 */
describe('running a version again', () => {
  it('replays the execution that produced it, under whatever is in force now', async () => {
    const backend = fakeBackend({
      ...routes,
      '/executions/e9/replay': {
        source_execution_id: 'e9',
        job: { id: 'job-77', job_type: 'align_voice', status: 'pending' },
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /run align_voice again/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    const replayed = backend.commands[0]!;
    expect(replayed.path).toBe('/executions/e9/replay');
    expect(replayed.body).toMatchObject({ actor_id: 'ada' });
    expect(await screen.findByRole('status')).toHaveTextContent('job-77');
  });

  it('says the original is untouched, because that is what makes it safe to press', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    const panel = await screen.findByTestId('rerun-version');
    expect(panel).toHaveTextContent(/nothing about the original is changed/i);
    expect(panel).toHaveTextContent(/resolved fresh/i);
  });
});

/**
 * Getting the article out (phase 16, found missing while auditing the API).
 *
 * Four export formats shipped in phase 13 and nothing called any of them, so a
 * finished article lived only in the blob store — the pipeline ran to
 * completion and there was no way to read the result outside the app.
 */
describe('exporting a version', () => {
  it('renders the version that passed validation, in a named format', async () => {
    const backend = fakeBackend({
      ...routes,
      '/versions/v3/export': {
        version_id: 'v3',
        content_hash: 'sha256:abc',
        format: 'markdown',
        media_type: 'text/markdown',
        content: '# Read-through caching\n\np99 fell to 120ms.',
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /export this version/i }));

    await waitFor(() =>
      expect(screen.getByLabelText(/exported article/i)).toHaveValue(
        '# Read-through caching\n\np99 fell to 120ms.',
      ),
    );
    const asked = backend.requests.find((request) => request.path === '/versions/v3/export');
    expect(asked?.query.get('format')).toBe('markdown');
  });

  it('shows the provenance beside the bytes, so a file can say where it came from', async () => {
    fakeBackend({
      ...routes,
      '/versions/v3/export': {
        version_id: 'v3',
        content_hash: 'sha256:abc',
        format: 'html',
        media_type: 'text/html',
        content: '<h1>Read-through caching</h1>',
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.selectOptions(await screen.findByLabelText(/^format$/i), 'html');
    await userEvent.click(screen.getByRole('button', { name: /export this version/i }));

    const panel = await screen.findByTestId('export');
    await waitFor(() => expect(panel).toHaveTextContent('sha256:abc'));
    expect(panel).toHaveTextContent('v3');
  });
});

describe('forking a version', () => {
  it('changes one variable and leaves the rest, so the two can be compared', async () => {
    const backend = fakeBackend({
      ...routes,
      '/executions/e9/fork': {
        source_execution_id: 'e9',
        job: { id: 'job-88', job_type: 'align_voice', status: 'pending' },
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.selectOptions(await screen.findByLabelText(/^change$/i), 'temperature');
    await userEvent.type(screen.getByLabelText(/^to$/i), '0.2');
    await userEvent.type(screen.getByLabelText(/^why$/i), 'the voice pass reads flat');
    await userEvent.click(screen.getByRole('button', { name: /fork this stage/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    const forked = backend.commands[0]!;
    expect(forked.path).toBe('/executions/e9/fork');
    expect(forked.body).toMatchObject({
      actor_id: 'ada',
      reason: 'the voice pass reads flat',
      variables: { temperature: '0.2' },
    });
  });

  it('will not fork without a value, because a fork that changes nothing is a replay', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    expect(await screen.findByRole('button', { name: /fork this stage/i })).toBeDisabled();
  });
});

describe('rerunning on a run that has finished', () => {
  it('says the result goes nowhere, instead of reading like a live rerun', async () => {
    fakeBackend({
      [`/articles/${ARTICLE_ID}/workspace`]: {
        ...articleWorkspace,
        producing_execution: {
          ...articleWorkspace.producing_execution,
          rerun_feeds_pipeline: false,
        },
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    // A replay never moves the run, which is what makes it safe to offer on a
    // finished one — and exactly why the finished case has to be said out loud.
    expect(await screen.findByTestId('rerun-dead-end')).toHaveTextContent(
      /nothing will score, validate or approve it/i,
    );
    expect(screen.getByRole('button', { name: /run align_voice again/i })).toBeEnabled();
  });

  it('says nothing of the sort while the run is still going', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await screen.findByTestId('rerun-version');

    expect(screen.queryByTestId('rerun-dead-end')).toBeNull();
  });
});

/**
 * Writing another of the approved articles (phase 16).
 *
 * Approving an architecture opens an article per approved concept and the run
 * carried exactly one of them to publication. The rest were rows nothing could
 * act on: the finished state is terminal, and artefacts are scoped to the run
 * that produced them, so a second run would have found no source model.
 */
describe('continuing to another article', () => {
  const withSiblings = {
    ...articleWorkspace,
    continue_command: {
      action: 'approve_and_continue',
      method: 'POST',
      path: `/articles/${ARTICLE_ID}/approve-and-continue`,
      requires_actor: true,
      taken_by: 'you',
    },
    siblings: [
      { id: 'a2', title: 'Artefacts Beat Chat Threads', status: 'draft', versions: 0 },
      { id: 'a3', title: 'Already Written', status: 'draft', versions: 3 },
    ],
  };

  it('offers only the concepts nothing has been written for', async () => {
    fakeBackend({ [`/articles/${ARTICLE_ID}/workspace`]: withSiblings });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);

    const panel = await screen.findByTestId('continue-to-next');
    expect(panel).toHaveTextContent('Artefacts Beat Chat Threads');
    // Three versions in: "another one" does not mean starting it over.
    expect(panel).not.toHaveTextContent('Already Written');
  });

  it('names the next article in the request, since the run cannot infer it', async () => {
    const backend = fakeBackend({
      [`/articles/${ARTICLE_ID}/workspace`]: withSiblings,
      [`/articles/${ARTICLE_ID}/approve-and-continue`]: {
        project_id: 'p1',
        run_id: 'r1',
        state: 'brief_generating',
        available_actions: [],
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.selectOptions(await screen.findByLabelText(/next article/i), 'a2');
    await userEvent.click(screen.getByRole('button', { name: /approve this and start the next/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]).toMatchObject({
      path: `/articles/${ARTICLE_ID}/approve-and-continue`,
      body: { actor_id: 'ada', next_article_id: 'a2' },
    });
  });

  it('stays hidden when every other concept has already been written', async () => {
    fakeBackend({
      [`/articles/${ARTICLE_ID}/workspace`]: { ...withSiblings, siblings: [] },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await screen.findByTestId('run-state');

    expect(screen.queryByTestId('continue-to-next')).toBeNull();
  });
});

/**
 * Sending a refused score back to be corrected (phase 16).
 *
 * The pause at `revision_required` had one exit and it published the article the
 * score had just refused. The choice inside the other exit matters: re-extracting
 * the same source cannot find a fact nobody ever wrote down.
 */
describe('routing a refused score', () => {
  const refused = {
    ...articleWorkspace,
    revise_command: {
      action: 'route_revision',
      method: 'POST',
      path: `/articles/${ARTICLE_ID}/revise`,
      requires_actor: true,
      taken_by: 'you',
    },
  };

  it('asks the policy to correct it, naming no destination', async () => {
    const backend = fakeBackend({
      [`/articles/${ARTICLE_ID}/workspace`]: refused,
      [`/articles/${ARTICLE_ID}/revise`]: {
        project_id: 'p1',
        run_id: 'r1',
        state: 'source_model_extracting',
        available_actions: [],
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.click(
      await screen.findByRole('button', { name: /correct it against the source/i }),
    );

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    // No `prefer`: which stage corrects a failure is the policy's call.
    expect(backend.commands[0]?.body).toEqual({ actor_id: 'ada' });
  });

  it('can ask for the questions instead, when the source never said it', async () => {
    const backend = fakeBackend({
      [`/articles/${ARTICLE_ID}/workspace`]: refused,
      [`/articles/${ARTICLE_ID}/revise`]: {
        project_id: 'p1',
        run_id: 'r1',
        state: 'source_questions_required',
        available_actions: [],
      },
    });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /ask me what is missing/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    expect(backend.commands[0]?.body).toMatchObject({
      actor_id: 'ada',
      prefer: 'source_questions_required',
    });
  });

  it('stays hidden when the run is not parked on a refused score', async () => {
    fakeBackend(routes);

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await screen.findByTestId('run-state');

    expect(screen.queryByTestId('route-revision')).toBeNull();
  });
});

/**
 * Deciding what the review found (plan/07 §8).
 *
 * The step between reviewing and planning, and until now unreachable: the ledger
 * that records these decisions had no service, no endpoint and no screen, so
 * every finding stayed `proposed`. A plan is built only from accepted findings,
 * an empty plan passes every check downstream, and the rewrite it produced was a
 * copy of the article — green the whole way.
 */
describe('deciding a review finding', () => {
  it('accepts one by the path the backend supplied', async () => {
    const backend = fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: reviewHistory });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} actor="ada" />);
    await userEvent.click(await screen.findByRole('button', { name: /^accept$/i }));

    expect(backend.commands[0]).toMatchObject({
      path: '/articles/art-1/findings/i2',
      body: { actor_id: 'ada', decision: 'accepted' },
    });
  });

  it('will not reject without a reason, because next round cannot tell why', async () => {
    fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: reviewHistory });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} actor="ada" />);
    const reject = await screen.findByRole('button', { name: /^reject$/i });

    expect(reject).toBeDisabled();
  });

  it('sends the reason with the rejection once there is one', async () => {
    const backend = fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: reviewHistory });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} actor="ada" />);
    const reason = await screen.findByLabelText(/why this does not apply/i);
    await userEvent.type(reason, 'the brief asks for it');
    await userEvent.click(await screen.findByRole('button', { name: /^reject$/i }));

    expect(backend.commands[0]).toMatchObject({
      path: '/articles/art-1/findings/i2',
      body: { decision: 'rejected', reason: 'the brief asks for it' },
    });
  });

  it('offers no control on a finding already decided', async () => {
    const decided = structuredClone(reviewHistory);
    decided.rounds![1]!.issues![0]!.decide_command = null;
    decided.rounds![1]!.issues![0]!.status = 'accepted';
    fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: decided });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} actor="ada" />);
    await screen.findAllByRole('article');

    expect(screen.queryByRole('button', { name: /^accept$/i })).toBeNull();
  });
});

/**
 * Handing a review over in one submission (IMPROVEMENTS §10).
 *
 * Triage was priced per finding: a request, a stage execution, and a full reload
 * of this screen — brief, version, diff, findings, plan, scores, lineage,
 * approval — for each one. The run of 2026-08-06 recorded 34 of them, five
 * seconds apart, for what the author was doing in one sitting. Of the ten
 * findings on that run, one changed the article; the rest existed to say no.
 *
 * What is asserted here is the count as much as the content. A version that
 * posted correctly but still posted five times would pass every assertion about
 * the body and fix nothing about the complaint.
 */
describe('triaging a review in one pass', () => {
  function undecided() {
    const workspace = structuredClone(articleWorkspace);
    const first = workspace.findings![0]!;
    workspace.findings = [
      { ...first, id: 'i1', ref: 'i1', status: 'proposed', decided_by: '' },
      { ...first, id: 'i2', ref: 'i2', status: 'proposed', decided_by: '', severity: 'optional' },
      { ...first, id: 'i3', ref: 'i3', status: 'proposed', decided_by: '', severity: 'optional' },
    ].map((finding) => ({
      ...finding,
      decide_command: {
        action: 'decide_finding',
        method: 'POST',
        path: `/articles/${ARTICLE_ID}/findings/${finding.id}`,
        requires_actor: true,
        taken_by: 'you',
      },
    }));
    workspace.triage_command = {
      action: 'triage_review',
      method: 'POST',
      path: `/articles/${ARTICLE_ID}/findings`,
      requires_actor: true,
      taken_by: 'you',
    };
    return workspace;
  }

  it('sends every decision in one request', async () => {
    const backend = fakeBackend({ [`/articles/${ARTICLE_ID}/workspace`]: undecided() });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    const accepts = await screen.findAllByRole('button', { name: /^accept$/i });
    for (const accept of accepts) await userEvent.click(accept);
    await userEvent.click(screen.getByRole('button', { name: /submit 3 decisions/i }));

    expect(backend.commands).toHaveLength(1);
    expect(backend.commands[0]).toMatchObject({
      path: `/articles/${ARTICLE_ID}/findings`,
      body: {
        actor_id: 'ada',
        decisions: [
          { finding_id: 'i1', decision: 'accepted' },
          { finding_id: 'i2', decision: 'accepted' },
          { finding_id: 'i3', decision: 'accepted' },
        ],
      },
    });
  });

  it('will not hand over a review with anything still undecided', async () => {
    fakeBackend({ [`/articles/${ARTICLE_ID}/workspace`]: undecided() });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    const accepts = await screen.findAllByRole('button', { name: /^accept$/i });
    await userEvent.click(accepts[0]!);

    expect(screen.getByRole('button', { name: /submit 1 decisions/i })).toBeDisabled();
    expect(screen.getByTestId('triage-count')).toHaveTextContent('1 of 3 decided, 2 still open');
  });

  it('rejects the rest for one reason, which is what five optional findings need', async () => {
    const backend = fakeBackend({ [`/articles/${ARTICLE_ID}/workspace`]: undecided() });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    const accepts = await screen.findAllByRole('button', { name: /^accept$/i });
    await userEvent.click(accepts[0]!);
    await userEvent.type(
      screen.getByLabelText(/reject the rest, for this reason/i),
      'the score no longer complains about these',
    );
    await userEvent.click(screen.getByRole('button', { name: /reject remaining 2/i }));
    await userEvent.click(screen.getByRole('button', { name: /submit 3 decisions/i }));

    expect(backend.commands).toHaveLength(1);
    expect(backend.commands[0]!.body).toMatchObject({
      decisions: [
        { finding_id: 'i1', decision: 'accepted' },
        {
          finding_id: 'i2',
          decision: 'rejected',
          reason: 'the score no longer complains about these',
        },
        {
          finding_id: 'i3',
          decision: 'rejected',
          reason: 'the score no longer complains about these',
        },
      ],
    });
  });

  it('leaves a decision already made alone when rejecting the rest', async () => {
    const backend = fakeBackend({ [`/articles/${ARTICLE_ID}/workspace`]: undecided() });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    const accepts = await screen.findAllByRole('button', { name: /^accept$/i });
    await userEvent.click(accepts[1]!);
    await userEvent.type(screen.getByLabelText(/reject the rest, for this reason/i), 'not this round');
    await userEvent.click(screen.getByRole('button', { name: /reject remaining 2/i }));
    await userEvent.click(screen.getByRole('button', { name: /submit 3 decisions/i }));

    const sent = backend.commands[0]!.body as { decisions: { finding_id: string; decision: string }[] };
    expect(sent.decisions.find((d) => d.finding_id === 'i2')).toMatchObject({
      decision: 'accepted',
    });
  });

  it('offers nothing to submit once the review has been worked through', async () => {
    // `triage_command` is withheld by the backend when nothing is undecided, so
    // the control is absent rather than present-and-disabled.
    fakeBackend({ [`/articles/${ARTICLE_ID}/workspace`]: articleWorkspace });

    render(<ArticleWorkspaceScreen articleId={ARTICLE_ID} actor="ada" />);
    await screen.findByRole('heading', { name: /findings/i });

    expect(screen.queryByTestId('triage')).toBeNull();
  });
});
