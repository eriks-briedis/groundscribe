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
import { render, screen } from '@testing-library/react';
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

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} />);

    const rounds = await screen.findAllByRole('article');
    expect(rounds[0]).toHaveTextContent(/revise/i);
    expect(rounds[0]).toHaveTextContent(/new/i);
    expect(rounds[1]).toHaveTextContent(/polish/i);
    expect(rounds[1]).toHaveTextContent(/repeated/i);
  });

  it('shows the score progression beside the rounds that earned it', async () => {
    fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: reviewHistory });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} />);

    const rows = await screen.findAllByRole('row');
    expect(rows.slice(1)[0]).toHaveTextContent('82');
    expect(rows.slice(1)[1]).toHaveTextContent('89.5');
  });

  it('passes on the stagnation warning the backend raised', async () => {
    fakeBackend({ [`/articles/${ARTICLE_ID}/reviews`]: reviewHistory });

    render(<ReviewHistoryScreen articleId={ARTICLE_ID} />);

    expect(await screen.findByRole('status')).toHaveTextContent(
      /two rounds have not moved the score/i,
    );
  });
});
