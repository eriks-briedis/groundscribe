/**
 * What changed between two versions (phase 11).
 *
 * The diff arrives computed, from the bodies as stored. This renders it and
 * recomputes nothing: a viewer that diffed the two strings itself would be a
 * second opinion about an artefact, and the two would disagree the first time
 * either side changed how it splits lines.
 */
import type { DiffView } from '@/api/client';

export interface DiffViewerProps {
  diff: DiffView | null | undefined;
}

export function DiffViewer({ diff }: DiffViewerProps) {
  if (!diff || (diff.lines ?? []).length === 0) {
    return <p className="diff diff--empty">No earlier version to compare against.</p>;
  }

  return (
    <div className="diff">
      <p className="diff__summary" data-testid="diff-summary">
        <span className="diff__added">+{diff.added ?? 0}</span>{' '}
        <span className="diff__removed">−{diff.removed ?? 0}</span> lines
      </p>
      <ol className="diff__lines">
        {(diff.lines ?? []).map((line, index) => (
          <li
            // The index is the identity here: two identical lines in one document
            // are two different lines of the diff, and nothing else distinguishes
            // them.
            key={`${line.kind}-${index}`}
            data-kind={line.kind}
            className={`diff__line diff__line--${line.kind}`}
          >
            <span className="diff__marker" aria-hidden="true">
              {line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ' '}
            </span>
            <span className="diff__text">{line.text}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
