/**
 * The debugging screens (phase 11).
 *
 * plan/11 → *Execution timeline*, *Stage inspector*, *Run comparison*, *Trace
 * filters*, and the mitigation that has to hold across all three: *progressive
 * disclosure — summary views by default, expandable raw payloads; separate
 * editorial vs debugging modes*.
 *
 * The filter tests check the thing that is easy to get wrong: filtering is the
 * backend's, so choosing one has to become a request rather than a local
 * `Array.filter` over rows already on screen.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ExecutionTimelineScreen } from './ExecutionTimelineScreen';
import { RunComparisonScreen } from './RunComparisonScreen';
import { StageInspectorScreen } from './StageInspectorScreen';
import { ModeProvider } from '@/app/mode';
import { fakeBackend, type RecordedRequest } from '@/test/backend';
import { comparison, EXECUTION_ID, inspection, PROJECT_ID, trace } from '@/test/fixtures';

describe('the execution timeline', () => {
  it('lists the run chronologically, with what each execution cost', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/trace`]: trace });

    render(<ExecutionTimelineScreen projectId={PROJECT_ID} />);

    const rows = await screen.findAllByRole('article');
    expect(rows[0]).toHaveTextContent('extract_source_truth');
    expect(rows[0]).toHaveTextContent('$0.012');
    expect(rows[1]).toHaveTextContent(/failed/i);
  });

  it('asks the backend to filter rather than filtering what it already has', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/trace`]: ({ query }: RecordedRequest) => ({
        ...trace,
        filters_applied: query.getAll('filter'),
        executions: trace.executions?.filter((execution) =>
          query.getAll('filter').every((name: string) => execution.matched_filters?.includes(name as never)),
        ),
      }),
    });

    render(<ExecutionTimelineScreen projectId={PROJECT_ID} />);
    await screen.findAllByRole('article');
    await userEvent.click(screen.getByRole('checkbox', { name: /failed/i }));

    await waitFor(() => expect(backend.requests).toHaveLength(2));
    expect(backend.requests[1]?.query.getAll('filter')).toEqual(['failed']);
    expect(await screen.findAllByRole('article')).toHaveLength(1);
  });

  it('offers every filter the contract knows, and no others', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/trace`]: trace });

    render(<ExecutionTimelineScreen projectId={PROJECT_ID} />);

    // The vocabulary comes from the response, so this asserts the screen renders
    // what the backend published rather than a list of its own.
    const names = (await screen.findAllByRole('checkbox')).map((box) => box.getAttribute('value'));
    expect(names).toEqual(trace.filters_available);
  });

  it('links each execution to its inspector', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/trace`]: trace });

    render(<ExecutionTimelineScreen projectId={PROJECT_ID} />);

    expect(await screen.findByRole('link', { name: /e2/ })).toHaveAttribute(
      'href',
      '#/executions/e2',
    );
  });
});

describe('the stage inspector', () => {
  it('opens on the summary, with the heavy payloads closed', async () => {
    fakeBackend({ [`/executions/${EXECUTION_ID}/inspect`]: inspection });

    render(
      <ModeProvider initial="editorial">
        <StageInspectorScreen executionId={EXECUTION_ID} />
      </ModeProvider>,
    );

    expect(await screen.findByRole('heading', { name: /extract_source_truth/ })).toBeInTheDocument();
    expect(screen.queryByText(/"messages"/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /effective request/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });

  it('opens every payload at once in debugging mode', async () => {
    fakeBackend({ [`/executions/${EXECUTION_ID}/inspect`]: inspection });

    render(
      <ModeProvider initial="debugging">
        <StageInspectorScreen executionId={EXECUTION_ID} />
      </ModeProvider>,
    );

    expect(await screen.findByRole('button', { name: /effective request/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByText(/Extract…/)).toBeInTheDocument();
  });

  it('shows every layer the backend recorded for the execution', async () => {
    fakeBackend({ [`/executions/${EXECUTION_ID}/inspect`]: inspection });

    render(
      <ModeProvider initial="editorial">
        <StageInspectorScreen executionId={EXECUTION_ID} />
      </ModeProvider>,
    );

    expect(await screen.findByTestId('inputs')).toHaveTextContent('source_document');
    expect(screen.getByTestId('outputs')).toHaveTextContent('source_model');
    expect(screen.getByTestId('context')).toHaveTextContent('over budget');
    expect(screen.getByTestId('invocations')).toHaveTextContent('llama3.1:70b-instruct');
    expect(screen.getByTestId('decisions')).toHaveTextContent('surfaced 1 of 2');
    expect(screen.getByTestId('events')).toHaveTextContent('stage.completed');
    expect(screen.getByTestId('cost')).toHaveTextContent('1200');
  });
});

describe('the run comparison', () => {
  it('puts the two executions side by side and marks what differs', async () => {
    fakeBackend({ '/executions/compare': comparison });

    render(<RunComparisonScreen left="e0" right="e2" />);

    const rows = await screen.findAllByRole('row');
    const status = rows.find((row) => row.textContent?.includes('status'));
    expect(status).toHaveAttribute('data-same', 'false');
    expect(status).toHaveTextContent('failed');
    expect(status).toHaveTextContent('succeeded');
    const stage = rows.find((row) => row.textContent?.includes('stage'));
    expect(stage).toHaveAttribute('data-same', 'true');
  });

  it('reports how far the two outputs sit apart', async () => {
    fakeBackend({ '/executions/compare': comparison });

    render(<RunComparisonScreen left="e0" right="e2" />);

    expect(await screen.findByTestId('edit-distance')).toHaveTextContent('14');
  });

  it('asks for exactly the pair it was given', async () => {
    const backend = fakeBackend({ '/executions/compare': comparison });

    render(<RunComparisonScreen left="e0" right="e2" />);
    await screen.findAllByRole('row');

    expect(backend.requests[0]?.query.get('left')).toBe('e0');
    expect(backend.requests[0]?.query.get('right')).toBe('e2');
  });
});
