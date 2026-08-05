/**
 * Running one stage again (phase 16, found missing while trying to use it).
 *
 * `POST /executions/{id}/replay` shipped in phase 12 and nothing called it, so
 * the only way to get a second draft was to start a project over. That is the
 * same gap the voice editor had, and it bit in the same place: after fixing the
 * voice profile there was no way to apply it to the article that had exposed the
 * problem.
 *
 * **A replay is not an edit.** The original execution, its artefacts and its
 * records are untouched; the rerun opens a new execution linked to it, and the
 * run's own state does not move (`transitions=not rerunning.is_rerun` in the
 * handlers). So this is safe to offer on a finished run, which is exactly when
 * somebody wants it.
 *
 * **A replay picks up what has changed around it.** It carries no variables, so
 * the voice, the rubric and the routing profile are resolved fresh rather than
 * pinned — a fork is the thing that holds one of them still. That is why this
 * button is the answer to "I fixed the voice, now do the article again" and a
 * fork would not be.
 */
import { useState } from 'react';

import { ApiError, sendCommand, type RerunResponse, type Schemas } from '@/api/client';

type ActionLink = Schemas['ActionLink'];

/**
 * The variables a fork may change, in the backend's own closed vocabulary.
 *
 * One at a time, and that is the design rather than a simplification: the point
 * of a fork is to make two runs comparable, and two runs differing in three
 * things are not. The backend refuses a name outside this list with a 422, so
 * this is a convenience for the person, not a validation.
 */
const VARIABLES = [
  { value: 'model', label: 'Model', hint: 'gpt-5-mini' },
  { value: 'provider', label: 'Provider', hint: 'ollama' },
  { value: 'temperature', label: 'Temperature', hint: '0.2' },
  { value: 'prompt_version', label: 'Prompt version', hint: 'v2' },
  { value: 'voice_profile', label: 'Voice profile version', hint: 'a stored version id' },
  { value: 'rubric_version', label: 'Rubric version', hint: '1.1' },
  { value: 'context_strategy', label: 'Context strategy', hint: 'relevance_ranked_source_segments' },
] as const;

export interface RerunProps {
  /**
   * Where the rerun is taken, as the backend addressed it.
   *
   * A link rather than an execution id this component turns into a URL: plan/11
   * forbids the frontend from addressing commands itself, and `guards.test.ts`
   * enforces it. Nothing renders when it is absent.
   */
  command?: ActionLink | null;
  /** Where to run it again with one thing changed. Absent hides the fork form. */
  forkCommand?: ActionLink | null;
  /** What is being run again, for the button to name. */
  stage?: string;
  /**
   * Whether the run will act on what this produces.
   *
   * The backend's answer, not a state this component reads: plan/11 forbids the
   * frontend branching on a workflow state and `guards.test.ts` enforces it. A
   * replay never moves the run — which is what makes it safe to offer on a
   * finished one — so on a finished run it writes a version nothing will ever
   * score, validate or approve. Saying so is the difference between a button
   * that works and one that appears to.
   */
  feedsPipeline?: boolean;
  actor: string;
  onQueued?: (rerun: RerunResponse) => void;
}

export function Rerun({
  command,
  forkCommand,
  stage,
  feedsPipeline = true,
  actor,
  onQueued,
}: RerunProps) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const [queued, setQueued] = useState<RerunResponse | null>(null);
  const [variable, setVariable] = useState<string>('model');
  const [value, setValue] = useState('');
  const [reason, setReason] = useState('');

  if (!command?.path) return null;

  async function fork() {
    if (!forkCommand?.path || !value.trim()) return;
    setBusy(true);
    setProblem('');
    try {
      const rerun = (await sendCommand(forkCommand.path, {
        actor_id: actor,
        reason,
        variables: { [variable]: value.trim() },
      })) as unknown as RerunResponse;
      setQueued(rerun);
      onQueued?.(rerun);
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function run() {
    if (!command?.path) return;
    setBusy(true);
    setProblem('');
    try {
      const rerun = (await sendCommand(
        command.path,
        command.requires_actor ? { actor_id: actor } : {},
      )) as unknown as RerunResponse;
      setQueued(rerun);
      onQueued?.(rerun);
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rerun" data-testid="rerun">
      <button type="button" onClick={run} disabled={busy}>
        {busy ? 'Queueing…' : stage ? `Run ${stage} again` : 'Run this stage again'}
      </button>
      <p className="muted">
        Runs the stage again under whatever is in force now — the voice, the rubric and the
        routing profile are resolved fresh. Nothing about the original is changed.
      </p>
      {feedsPipeline ? null : (
        <p className="warning" data-testid="rerun-dead-end">
          This run has finished, and a rerun does not restart it. What comes out is a version
          you can read and export, and nothing will score, validate or approve it. To carry an
          article further, start a new run.
        </p>
      )}
      {queued ? (
        <p role="status">
          Queued as job {queued.job.id}.{' '}
          {feedsPipeline
            ? 'It becomes a new version beside the one it came from, and the run carries on.'
            : 'It becomes a new version beside the one it came from, and stops there.'}
        </p>
      ) : null}
      {forkCommand?.path ? (
        <details data-testid="fork">
          <summary>…or change one thing and compare</summary>
          <p className="muted">
            One variable, deliberately: two runs differing in three things cannot be compared.
            Everything else stays as it was, which is the opposite of a plain rerun.
          </p>
          <label>
            Change
            <select value={variable} onChange={(event) => setVariable(event.target.value)}>
              {VARIABLES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            To
            <input
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder={VARIABLES.find((item) => item.value === variable)?.hint}
            />
          </label>
          <label>
            Why
            <input value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
          <button type="button" onClick={fork} disabled={busy || !value.trim()}>
            Fork this stage
          </button>
        </details>
      ) : null}

      {problem ? (
        <p className="failure" role="alert">
          {problem}
        </p>
      ) : null}
    </div>
  );
}
