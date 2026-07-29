/**
 * The shared artefact primitives (phase 11).
 *
 * plan/11 → *Diff + score-history rendering (component): version diffs and the
 * score-progression table render from backend data*, and *progressive
 * disclosure: summary views by default, expandable raw payloads*.
 *
 * "From backend data" is the load-bearing part. The diff is computed by the
 * backend from the stored bodies, so the viewer's job is to show it and not to
 * recompute it; a viewer that diffed the two strings itself would disagree with
 * the artefact the moment either changed.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { Disclosure } from './Disclosure';
import { DiffViewer } from './DiffViewer';
import { ScoreTable } from './ScoreTable';
import { articleWorkspace, reviewHistory } from '@/test/fixtures';

describe('the diff viewer', () => {
  it('renders the lines the backend marked, in the order it marked them', () => {
    render(<DiffViewer diff={articleWorkspace.diff ?? { lines: [] }} />);

    const lines = screen.getAllByRole('listitem');
    expect(lines.map((line) => line.dataset.kind)).toEqual(['equal', 'removed', 'added']);
    expect(lines[1]).toHaveTextContent('That number is the reason anyone would read this.');
    expect(lines[2]).toHaveTextContent('That number is why anyone would read this.');
  });

  it('summarises the size of the change without recounting it', () => {
    render(<DiffViewer diff={articleWorkspace.diff ?? { lines: [] }} />);

    expect(screen.getByTestId('diff-summary')).toHaveTextContent('+1');
    expect(screen.getByTestId('diff-summary')).toHaveTextContent('−1');
  });

  it('says when there is nothing to compare against', () => {
    render(<DiffViewer diff={null} />);

    expect(screen.getByText(/no earlier version/i)).toBeInTheDocument();
  });
});

describe('the score table', () => {
  it('shows the progression, oldest first, with the rubric each was scored under', () => {
    render(<ScoreTable scores={reviewHistory.scores ?? []} />);

    const rows = screen.getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('82');
    expect(rows[0]).toHaveTextContent(/failed/i);
    expect(rows[1]).toHaveTextContent('89.5');
    expect(rows[1]).toHaveTextContent(/passed/i);
    expect(rows[1]).toHaveTextContent('1.0');
  });

  it('shows why a score failed, because a number alone cannot be argued with', () => {
    render(<ScoreTable scores={reviewHistory.scores ?? []} />);

    expect(screen.getByText(/factual_fidelity below its minimum/i)).toBeInTheDocument();
  });

  it('says when nothing has been scored yet rather than showing an empty table', () => {
    render(<ScoreTable scores={[]} />);

    expect(screen.getByText(/not scored yet/i)).toBeInTheDocument();
  });
});

describe('progressive disclosure', () => {
  it('shows the summary and hides the payload until it is asked for', async () => {
    render(
      <Disclosure summary="effective request">
        <pre>the whole prompt</pre>
      </Disclosure>,
    );

    expect(screen.getByText(/effective request/i)).toBeInTheDocument();
    expect(screen.queryByText(/the whole prompt/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /effective request/i }));

    expect(screen.getByText(/the whole prompt/i)).toBeInTheDocument();
  });
});
