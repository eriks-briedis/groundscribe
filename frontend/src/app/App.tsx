/**
 * The shell (phase 11).
 *
 * Navigation is the address bar and nothing else: `#/projects/…`,
 * `#/articles/…`, `#/executions/…`. A hash router in forty lines rather than a
 * dependency, because the app is a handful of artefact screens and every route
 * is "show me this thing" — there is no nesting, no guarding, and no
 * redirecting to be had, and a router that could express those would invite
 * someone to write them.
 *
 * The mode toggle lives here because it is the one control that belongs to the
 * whole app: editorial by default, debugging when a person is looking for what
 * went wrong (plan/11 → trace overload).
 */
import { useEffect, useState } from 'react';

import { ModeProvider, useMode } from './mode';
import { ArchitectureBoardScreen } from '@/screens/ArchitectureBoardScreen';
import { ArticleWorkspaceScreen } from '@/screens/ArticleWorkspaceScreen';
import { DashboardScreen } from '@/screens/DashboardScreen';
import { ExecutionTimelineScreen } from '@/screens/ExecutionTimelineScreen';
import { QuestionQueueScreen } from '@/screens/QuestionQueueScreen';
import { ReviewHistoryScreen } from '@/screens/ReviewHistoryScreen';
import { RunComparisonScreen } from '@/screens/RunComparisonScreen';
import { SourceWorkspaceScreen } from '@/screens/SourceWorkspaceScreen';
import { StageInspectorScreen } from '@/screens/StageInspectorScreen';

/**
 * Who is acting.
 *
 * Local-first and single-author for now: the spec's product is one person's
 * writing pipeline, and phase 13 is where identity becomes a question worth
 * asking. Kept in one place so that phase has one thing to replace.
 */
const ACTOR = 'ada';

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const update = () => setHash(window.location.hash);
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  return hash;
}

function Screen({ hash }: { hash: string }) {
  const [route, ...rest] = hash.replace(/^#\/?/, '').split('/');
  const [id, tail] = rest;

  if (route === 'projects' && id) {
    if (tail === 'source') return <SourceWorkspaceScreen projectId={id} />;
    if (tail === 'questions') return <QuestionQueueScreen projectId={id} actor={ACTOR} />;
    if (tail === 'architecture') return <ArchitectureBoardScreen projectId={id} />;
    if (tail === 'trace') return <ExecutionTimelineScreen projectId={id} />;
    return <DashboardScreen projectId={id} actor={ACTOR} />;
  }
  if (route === 'articles' && id) {
    if (tail === 'reviews') return <ReviewHistoryScreen articleId={id} />;
    return <ArticleWorkspaceScreen articleId={id} actor={ACTOR} />;
  }
  if (route === 'executions' && id) {
    return <StageInspectorScreen executionId={id} />;
  }
  if (route === 'compare') {
    const query = new URLSearchParams(hash.split('?')[1] ?? '');
    const left = query.get('left');
    const right = query.get('right');
    if (left && right) return <RunComparisonScreen left={left} right={right} />;
  }
  return (
    <section className="screen">
      <h1>Nothing here</h1>
      <p>
        Open a project at <code>#/projects/&lt;id&gt;</code>.
      </p>
    </section>
  );
}

function Chrome() {
  const hash = useHash();
  const { mode, setMode } = useMode();

  return (
    <div className="app">
      <header className="app__header">
        <a className="app__brand" href="#/">
          groundscribe
        </a>
        <span className="app__mode" data-testid="mode">
          {mode}
        </span>
        <button
          type="button"
          onClick={() => setMode(mode === 'editorial' ? 'debugging' : 'editorial')}
        >
          {mode === 'editorial' ? 'switch to debugging' : 'switch to editorial'}
        </button>
      </header>
      <main className="app__main">
        <Screen hash={hash} />
      </main>
    </div>
  );
}

export function App() {
  return (
    <ModeProvider>
      <Chrome />
    </ModeProvider>
  );
}
