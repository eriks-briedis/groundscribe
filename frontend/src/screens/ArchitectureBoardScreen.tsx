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

import { fetchArchitecture, type ArchitectureBoard } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { Payload } from '@/components/Disclosure';

export interface ArchitectureBoardScreenProps {
  projectId: string;
}

export function ArchitectureBoardScreen({ projectId }: ArchitectureBoardScreenProps) {
  const resource = useResource<ArchitectureBoard>(
    () => fetchArchitecture(projectId),
    [projectId],
  );
  const [comparing, setComparing] = useState(false);

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

            <button type="button" onClick={() => setComparing((value) => !value)}>
              {comparing ? 'hide versions' : 'compare versions'}
            </button>

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
