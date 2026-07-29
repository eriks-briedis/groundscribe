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
import { useCallback, useEffect, useState } from 'react';

import { ModeProvider, useMode } from './mode';
import { fetchSession, onUnauthorized, signOut } from '@/api/client';
import { SignInScreen } from '@/screens/SignInScreen';
import { ArchitectureBoardScreen } from '@/screens/ArchitectureBoardScreen';
import { ArticleWorkspaceScreen } from '@/screens/ArticleWorkspaceScreen';
import { DashboardScreen } from '@/screens/DashboardScreen';
import { ExecutionTimelineScreen } from '@/screens/ExecutionTimelineScreen';
import { HomeScreen } from '@/screens/HomeScreen';
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
    if (tail === 'architecture') return <ArchitectureBoardScreen projectId={id} actor={ACTOR} />;
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
  if (route === '' || route === 'projects') {
    // The list, which is also what an empty address means: an application whose
    // front door is "paste an id" has no front door.
    return <HomeScreen actor={ACTOR} />;
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

/**
 * Whether this browser is signed in, and a way to be told when it stops being.
 *
 * The answer comes from the backend because the cookie is `HttpOnly`. Any
 * request can be the one that discovers a lapsed session, so the client reports
 * that centrally and the whole app returns to the form — a screen left showing
 * artefacts it can no longer refresh would be lying about what it knows.
 */
function useSession() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);

  const check = useCallback(() => {
    fetchSession()
      .then((session) => setAuthenticated(session.authenticated))
      .catch(() => setAuthenticated(false));
  }, []);

  useEffect(() => {
    check();
    onUnauthorized(() => setAuthenticated(false));
  }, [check]);

  return { authenticated, check, setAuthenticated };
}

function Chrome() {
  const hash = useHash();
  const { mode, setMode } = useMode();
  const session = useSession();

  if (session.authenticated === null) {
    return <p className="loading">Loading…</p>;
  }
  if (!session.authenticated) {
    return <SignInScreen onSignedIn={session.check} />;
  }

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
        <button
          type="button"
          onClick={() => {
            void signOut().finally(() => session.setAuthenticated(false));
          }}
        >
          sign out
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
