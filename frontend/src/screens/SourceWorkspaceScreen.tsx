/**
 * The source workspace (phase 11).
 *
 * plan/11 → *sources, extracted facts, claims, evidence, unknowns, confidential
 * material, provenance links, provider-visibility rules*.
 *
 * The claim is the unit here, and each one is shown with the segments it rests
 * on, because a claim whose evidence a person cannot open is a claim they have
 * to take on trust — which is the opposite of what the source model is for.
 *
 * It is also where material goes *in*. Ingestion is a command with no workflow
 * action behind it — nothing about a project's state makes importing legal or
 * illegal — so the action bar never mentions it, and a screen that only listed
 * what had been imported left a new project with nothing to do.
 */
import { useState, type FormEvent } from 'react';

import {
  ApiError,
  fetchSourceWorkspace,
  sendCommand,
  type Schemas,
  type SourceWorkspace,
} from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { Disclosure, Payload } from '@/components/Disclosure';

/** How a source arrived, which is what decides how it is segmented. */
const FORMATS = [
  { value: 'markdown', label: 'Markdown' },
  { value: 'plain_text', label: 'plain text' },
  { value: 'pasted_notes', label: 'pasted notes' },
] as const;

export interface SourceWorkspaceScreenProps {
  projectId: string;
}

export function SourceWorkspaceScreen({ projectId }: SourceWorkspaceScreenProps) {
  const resource = useResource<SourceWorkspace>(
    () => fetchSourceWorkspace(projectId),
    [projectId],
  );

  return (
    <Loaded resource={resource}>
      {(workspace) => {
        // Every segment in the project, so a claim can show the words behind it
        // rather than the ids it happens to cite.
        const segments = new Map(
          (workspace.documents ?? []).flatMap((document) =>
            (document.segments ?? []).map((segment) => [segment.id, segment]),
          ),
        );

        return (
          <section className="screen screen--source">
            <h1>Source</h1>

            <section className="panel">
              <h2>Visibility</h2>
              <p data-testid="visibility">
                Providers allowed: {workspace.provider_visibility.allowed_providers?.join(', ') || 'none'}
                {' · '}
                Never sent: {workspace.provider_visibility.confidential_names?.join(', ') || 'nothing marked'}
                {' · '}
                Trace retention {workspace.provider_visibility.trace_retention_consent ? 'agreed' : 'refused'}
              </p>
            </section>

            <ImportSource
              command={workspace.import_command}
              onImported={() => resource.reload()}
            />

            <section className="panel">
              <h2>Documents</h2>
              {(workspace.documents ?? []).length === 0 ? (
                <p>No source material yet. Add some above — everything else follows from it.</p>
              ) : null}
              {(workspace.documents ?? []).map((document) => (
                <article key={document.id} data-testid={`document-${document.id}`} className="card">
                  <h3>{document.title}</h3>
                  <p>
                    {document.source_format} · {(document.segments ?? []).length} segments ·{' '}
                    {document.content_hash}
                    {document.confidential ? ' · confidential' : ''}
                  </p>
                  <Disclosure summary={`${(document.segments ?? []).length} segments`}>
                    <ol className="segments">
                      {(document.segments ?? []).map((segment) => (
                        <li key={segment.id} data-testid={`segment-${segment.id}`}>
                          <span className="tag">{segment.kind}</span> {segment.text}
                        </li>
                      ))}
                    </ol>
                  </Disclosure>
                </article>
              ))}
            </section>

            <section className="panel">
              <h2>Claims</h2>
              <ul className="claims">
                {(workspace.claims ?? []).map((claim) => (
                  <li key={claim.id} data-testid={`claim-${claim.id}`}>
                    <p>
                      {claim.text} <span className="tag">{claim.classification}</span>
                    </p>
                    <Disclosure summary={`evidence (${(claim.segment_ids ?? []).length})`}>
                      <ul>
                        {(claim.segment_ids ?? []).map((id) => (
                          <li key={id}>{segments.get(id)?.text ?? `segment ${id} (not loaded)`}</li>
                        ))}
                      </ul>
                    </Disclosure>
                  </li>
                ))}
              </ul>
            </section>

            <section className="panel">
              <h2>Unknowns</h2>
              <ul>
                {(workspace.unknowns ?? []).map((unknown) => (
                  <li key={unknown.id}>
                    {unknown.question} <span className="tag">{unknown.priority}</span>
                  </li>
                ))}
              </ul>
            </section>

            <section className="panel">
              <h2>Structured source</h2>
              <p>
                Built by{' '}
                {workspace.provenance.source_model_execution_id ? (
                  <a href={`#/executions/${workspace.provenance.source_model_execution_id}`}>
                    {workspace.provenance.source_model_execution_id}
                  </a>
                ) : (
                  'nothing yet'
                )}
              </p>
              <Payload label="source model" value={workspace.source_model} />
            </section>
          </section>
        );
      }}
    </Loaded>
  );
}

/**
 * A file's contents, as text.
 *
 * `FileReader` rather than `Blob.text()`, which every browser has and jsdom does
 * not: the newer call would work everywhere the app runs and nowhere the app is
 * tested, which is the worst of both — the test would have to pretend, and the
 * pretence is what would eventually be wrong.
 */
function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error(`could not read ${file.name}`));
    reader.readAsText(file);
  });
}

interface ImportSourceProps {
  command: Schemas['ActionLink'] | null | undefined;
  onImported: () => void;
}

/**
 * Put one piece of material into the project.
 *
 * A file is read into the form rather than posted straight off: a person should
 * see what they are about to send and give it a name, and the same box is where
 * pasted notes go — which is the other half of what "a source" means here.
 */
function ImportSource({ command, onImported }: ImportSourceProps) {
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [format, setFormat] = useState<string>('markdown');
  const [confidential, setConfidential] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const takeFile = async (file: File | undefined) => {
    if (!file) return;
    setText(await readAsText(file));
    setTitle((current) => current || file.name);
  };

  const add = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      await sendCommand(
        command?.path ?? '',
        { title, text, source_format: format, confidential },
        command?.method ?? 'POST',
      );
      setTitle('');
      setText('');
      setConfidential(false);
      onImported();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="panel import-source" onSubmit={(event) => void add(event)}>
      <h2>Add source material</h2>

      <label htmlFor="source-file">Choose a file</label>
      <input
        id="source-file"
        type="file"
        accept=".md,.markdown,.txt,text/*"
        onChange={(event) => void takeFile(event.target.files?.[0])}
      />

      <label htmlFor="source-title">Document title</label>
      <input
        id="source-title"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        required
      />

      <label htmlFor="source-text">Text</label>
      <textarea
        id="source-text"
        value={text}
        onChange={(event) => setText(event.target.value)}
        rows={10}
        required
      />

      <label htmlFor="source-format">Format</label>
      <select
        id="source-format"
        value={format}
        onChange={(event) => setFormat(event.target.value)}
      >
        {FORMATS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={confidential}
          onChange={(event) => setConfidential(event.target.checked)}
        />
        Confidential — never send this to a provider
      </label>

      <button type="submit" disabled={busy}>
        {busy ? 'adding…' : 'add this source'}
      </button>
      {problem ? (
        <p role="alert" className="failure">
          {problem}
        </p>
      ) : null}
    </form>
  );
}
