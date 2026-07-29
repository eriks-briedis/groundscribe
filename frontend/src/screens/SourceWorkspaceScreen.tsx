/**
 * The source workspace (phase 11).
 *
 * plan/11 → *sources, extracted facts, claims, evidence, unknowns, confidential
 * material, provenance links, provider-visibility rules*.
 *
 * The claim is the unit here, and each one is shown with the segments it rests
 * on, because a claim whose evidence a person cannot open is a claim they have
 * to take on trust — which is the opposite of what the source model is for.
 */
import { fetchSourceWorkspace, type SourceWorkspace } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { Disclosure, Payload } from '@/components/Disclosure';

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

            <section className="panel">
              <h2>Documents</h2>
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
