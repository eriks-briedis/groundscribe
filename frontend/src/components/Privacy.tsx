/**
 * Taking the trace out, and destroying it (phase 16, found missing while auditing).
 *
 * Both endpoints shipped in phase 13 and neither was reachable, which meant the
 * local-first story was true of the code and not of the product: a person could
 * not get their own records out, and could not delete them.
 *
 * **The warning comes before the artefact.** A full export of a project holding
 * confidential material is refused by the backend — 409 — unless the caller
 * acknowledges it, because by the time a warning field on a 200 could be read
 * the bytes are already on disk. This screen says so first, and the
 * acknowledgement is a separate deliberate act rather than a checkbox that
 * happens to be next to the button.
 *
 * **Deletion is of content, not of history.** Trace events are append-only by
 * construction, so "delete my traces" cannot mean "make it look like nothing
 * ran" — and should not: the record that a call happened is what makes every
 * cost and repair-rate figure computed from it true. The result says what went
 * and what stayed, including payloads left alone because another project shares
 * them.
 */
import { useState } from 'react';

import {
  ApiError,
  exportTraces,
  sendCommand,
  type Schemas,
  type TraceDeletion,
  type TraceExport,
} from '@/api/client';

type ActionLink = Schemas['ActionLink'];

export interface PrivacyProps {
  projectId: string;
  privacy: Schemas['PrivacyView'];
}

export function Privacy({ projectId, privacy }: PrivacyProps) {
  const [exported, setExported] = useState<TraceExport | null>(null);
  const [deleted, setDeleted] = useState<TraceDeletion | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [problem, setProblem] = useState('');
  const [busy, setBusy] = useState(false);

  async function runExport(options: { sanitise?: boolean; acknowledged?: boolean }) {
    setBusy(true);
    setProblem('');
    try {
      setExported(await exportTraces(projectId, options));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function runDelete(command: ActionLink) {
    if (!command.path) return;
    setBusy(true);
    setProblem('');
    try {
      setDeleted((await sendCommand(command.path, {}, 'DELETE')) as unknown as TraceDeletion);
      setConfirming(false);
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="privacy" data-testid="privacy">
      <p className="muted">
        Retention: {privacy.retention_mode.replace(/_/g, ' ') || 'not set'}.{' '}
        {privacy.holds_confidential
          ? 'This project holds material marked confidential.'
          : 'Nothing here is marked confidential.'}
      </p>

      <div className="actions">
        <button type="button" onClick={() => runExport({ sanitise: true })} disabled={busy}>
          Export, sanitised
        </button>
        <button type="button" onClick={() => runExport({})} disabled={busy}>
          Export in full
        </button>
      </div>
      {privacy.holds_confidential ? (
        <p className="warning">
          A full export carries the confidential material. The backend refuses it until you say
          so explicitly.
        </p>
      ) : null}

      {problem ? (
        <>
          <p className="failure" role="alert">
            {problem}
          </p>
          <button type="button" onClick={() => runExport({ acknowledged: true })} disabled={busy}>
            Export anyway, confidential material included
          </button>
        </>
      ) : null}

      {exported ? (
        <p role="status" data-testid="export-result">
          {exported.sanitised ? 'Sanitised export' : 'Full export'}: {exported.runs.length} run
          {exported.runs.length === 1 ? '' : 's'}, {exported.withheld_payloads} payload
          {exported.withheld_payloads === 1 ? '' : 's'} withheld.
          {exported.warnings.length ? ` ${exported.warnings.join(' ')}` : ''}
        </p>
      ) : null}

      <hr />

      {confirming ? (
        <div data-testid="confirm-delete">
          <p className="warning">
            This drops the stored prompts and responses for good. The record that each call
            happened stays, so cost and repair figures remain true.
          </p>
          <div className="actions">
            <button
              type="button"
              onClick={() => privacy.delete_command && runDelete(privacy.delete_command)}
              disabled={busy}
            >
              Delete them
            </button>
            <button type="button" onClick={() => setConfirming(false)}>
              Keep them
            </button>
          </div>
        </div>
      ) : (
        <button type="button" onClick={() => setConfirming(true)} disabled={!privacy.delete_command}>
          Delete this project&apos;s traces
        </button>
      )}

      {deleted ? (
        <p role="status" data-testid="delete-result">
          Removed {deleted.payloads} payload{deleted.payloads === 1 ? '' : 's'} (
          {deleted.bytes_reclaimed} bytes). Kept {deleted.records_kept} record
          {deleted.records_kept === 1 ? '' : 's'}; left {deleted.shared_payloads} shared with
          another project.
        </p>
      ) : null}
    </div>
  );
}
