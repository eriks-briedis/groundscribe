/**
 * The architecture board (phase 11).
 *
 * plan/11 → *article-concept cards with … approve/compare-versions*.
 *
 * Versions are first-class here rather than a history tab: an approved
 * architecture is locked, and changing it forks a new version that names who
 * authorised it (plan/05). A board that showed only the current shape would hide
 * the fact that there had been another one.
 */
import { useState } from 'react';

import { fetchArchitecture, sendCommand, type ArchitectureBoard } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { Payload } from '@/components/Disclosure';

export interface ArchitectureBoardScreenProps {
  projectId: string;
  actor: string;
}

export function ArchitectureBoardScreen({ projectId, actor }: ArchitectureBoardScreenProps) {
  const resource = useResource<ArchitectureBoard>(
    () => fetchArchitecture(projectId),
    [projectId],
  );
  const [comparing, setComparing] = useState(false);
  const [editing, setEditing] = useState(false);

  return (
    <Loaded resource={resource}>
      {(board) => {
        const current = (board.versions ?? []).find(
          (version) => version.id === board.current_version_id,
        );

        return (
          <section className="screen screen--architecture">
            <h1>Architecture</h1>
            <p data-testid="current-version">
              {current ? current.id : 'nothing proposed yet'}
              {current?.locked ? ` · locked by ${current.locked_by}` : ' · not approved'}
            </p>

            <ul className="cards">
              {(current?.concepts ?? []).map((concept) => (
                <li key={concept.id} className="card" data-testid={`concept-${concept.id}`}>
                  <h2>
                    <a href={`#/articles/${concept.id}`}>{concept.title}</a>
                  </h2>
                  <p className="muted">{concept.angle}</p>
                  <p>{concept.thesis}</p>
                </li>
              ))}
            </ul>

            <div className="actions">
              <button type="button" onClick={() => setComparing((value) => !value)}>
                {comparing ? 'hide versions' : 'compare versions'}
              </button>
              {board.edit_command ? (
                <button type="button" onClick={() => setEditing((value) => !value)}>
                  {editing ? 'stop editing' : 'edit the architecture'}
                </button>
              ) : null}
              {board.approve_command ? (
                <Approve
                  path={board.approve_command.path ?? ''}
                  actor={actor}
                  onDone={() => resource.reload()}
                />
              ) : null}
            </div>

            {editing && board.edit_command ? (
              <EditForm
                path={board.edit_command.path ?? ''}
                method={board.edit_command.method ?? 'PUT'}
                operations={board.operations ?? []}
                concepts={current?.concepts ?? []}
                actor={actor}
                onDone={() => {
                  setEditing(false);
                  resource.reload();
                }}
              />
            ) : null}

            {comparing ? (
              <section className="panel versions">
                {(board.versions ?? []).map((version) => (
                  <article key={version.id} data-testid={`version-${version.id}`} className="card">
                    <h3>
                      {version.id}
                      {version.parent_id ? ` (from ${version.parent_id})` : ''}
                    </h3>
                    <p>{version.summary}</p>
                    <ul>
                      {(version.concepts ?? []).map((concept) => (
                        <li key={concept.id}>{concept.title}</li>
                      ))}
                    </ul>
                  </article>
                ))}
              </section>
            ) : null}

            <Payload label="the proposal, as the model returned it" value={board.proposal} />
          </section>
        );
      }}
    </Loaded>
  );
}

function Approve({ path, actor, onDone }: { path: string; actor: string; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  return (
    <>
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          setProblem(null);
          sendCommand(path, { actor_id: actor })
            .then(onDone)
            .catch((error: unknown) => setProblem(String(error)))
            .finally(() => setBusy(false));
        }}
      >
        approve architecture
      </button>
      {problem ? <p role="alert">{problem}</p> : null}
    </>
  );
}

interface EditFormProps {
  path: string;
  method: string;
  operations: readonly string[];
  //: `ref` as well as `id`, because an edit names the proposal's label and a
  //: link names the row. Neither is derivable from the other.
  concepts: readonly { id: string; ref: string; title: string }[];
  actor: string;
  onDone: () => void;
}

/**
 * One override, submitted as the backend describes it.
 *
 * The operations come from the response and the fields are filled in for
 * whichever is chosen; the form does not know that a rename needs a title and a
 * merge does not, beyond offering the inputs. Phase 06 validates the command and
 * refuses one that does not make sense, which is the check that matters — a form
 * that enforced its own version of those rules would be a second opinion about
 * what an override may do.
 */
function EditForm({ path, method, operations, concepts, actor, onDone }: EditFormProps) {
  const [operation, setOperation] = useState(operations[0] ?? '');
  const [selected, setSelected] = useState<string[]>([]);
  const [title, setTitle] = useState('');
  const [thesis, setThesis] = useState('');
  const [claims, setClaims] = useState('');
  const [reason, setReason] = useState('');
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async () => {
    setProblem(null);
    const command: Record<string, unknown> = { operation, article_ids: selected };
    if (title) command.title = title;
    if (thesis) command.thesis = thesis;
    if (claims) command.claim_ids = claims.split(/[,\s]+/).filter(Boolean);
    try {
      await sendCommand(path, { commands: [command], requested_by: actor, reason }, method);
      onDone();
    } catch (error) {
      setProblem(String(error));
    }
  };

  return (
    <form
      className="panel edit-form"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label>
        Operation
        <select value={operation} onChange={(event) => setOperation(event.target.value)}>
          {operations.map((name) => (
            <option key={name} value={name}>
              {name.replace(/_/g, ' ')}
            </option>
          ))}
        </select>
      </label>

      <fieldset>
        <legend>Which articles</legend>
        {/* Selected by `ref`, not by `id`. An override is applied to the
            proposal document, whose articles are A1…An; the row id addresses
            the article the proposal opened and means nothing to the edit. */}
        {concepts.map((concept) => (
          <label key={concept.id}>
            <input
              type="checkbox"
              value={concept.ref}
              checked={selected.includes(concept.ref)}
              onChange={() =>
                setSelected((current) =>
                  current.includes(concept.ref)
                    ? current.filter((ref) => ref !== concept.ref)
                    : [...current, concept.ref],
                )
              }
            />
            {concept.title}
          </label>
        ))}
      </fieldset>

      <label>
        New title
        <input value={title} onChange={(event) => setTitle(event.target.value)} />
      </label>
      <label>
        New thesis
        <input value={thesis} onChange={(event) => setThesis(event.target.value)} />
      </label>
      <label>
        Claim ids
        <input value={claims} onChange={(event) => setClaims(event.target.value)} />
      </label>
      <label>
        Why
        <input value={reason} onChange={(event) => setReason(event.target.value)} />
      </label>

      <button type="submit">submit the edit</button>
      {problem ? <p role="alert">{problem}</p> : null}
    </form>
  );
}
