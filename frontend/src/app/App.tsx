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
 * What the router does not decide, the header does: a project has five screens
 * and they used to be reachable only by typing the URL or by finding a link at
 * the bottom of the dashboard. Screens are the interface's own idea — the
 * backend has artefacts, not tabs — so the list of them lives here, and each one
 * still shows only what the backend gave it.
 *
 * Two controls belong to the whole app rather than to any screen: the editorial/
 * debugging mode (plan/11 → trace overload) and the colour theme.
 */
import { useCallback, useEffect, useState } from 'react';

import { ModeProvider, useMode } from './mode';
import { ThemeProvider, ThemeToggle } from './theme';
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

/** A project's screens, in the order the work moves through them. */
const PROJECT_SCREENS: readonly { tail: string; label: string }[] = [
  { tail: '', label: 'Overview' },
  { tail: 'source', label: 'Source' },
  { tail: 'questions', label: 'Questions' },
  { tail: 'architecture', label: 'Architecture' },
  { tail: 'trace', label: 'Timeline' },
];

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const update = () => setHash(window.location.hash);
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  return hash;
}

function parse(hash: string): { route: string; id?: string; tail?: string } {
  const [route = '', ...rest] = hash.replace(/^#\/?/, '').split('/');
  const [id, tail] = rest;
  return { route, id, tail: (tail ?? '').split('?')[0] ?? '' };
}

function Screen({ hash }: { hash: string }) {
  const { route, id, tail } = parse(hash);

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
      <p className="muted">
        Open a project at <code>#/projects/&lt;id&gt;</code>, or start one from{' '}
        <a href="#/">the project list</a>.
      </p>
    </section>
  );
}

/** The project's screens, when a project is what is on screen. */
function ProjectNav({ hash }: { hash: string }) {
  const { route, id, tail } = parse(hash);
  if (route !== 'projects' || !id) return null;

  return (
    <nav className="projectnav" aria-label="Project screens">
      {PROJECT_SCREENS.map((screen) => (
        <a
          key={screen.tail || 'overview'}
          className="projectnav__link"
          aria-current={(tail ?? '') === screen.tail ? 'page' : undefined}
          href={`#/projects/${id}${screen.tail ? `/${screen.tail}` : ''}`}
        >
          {screen.label}
        </a>
      ))}
    </nav>
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
        <span className="app__spacer" />
        <div className="app__tools">
          <ThemeToggle />
          <span className="app__mode" data-testid="mode">
            {mode}
          </span>
          <button
            type="button"
            onClick={() => setMode(mode === 'editorial' ? 'debugging' : 'editorial')}
          >
            {mode === 'editorial' ? 'Switch to debugging' : 'Switch to editorial'}
          </button>
          <button
            type="button"
            onClick={() => {
              void signOut().finally(() => session.setAuthenticated(false));
            }}
          >
            Sign out
          </button>
        </div>
      </header>
      <main className="app__main">
        <ProjectNav hash={hash} />
        <Screen hash={hash} />
      </main>
    </div>
  );
}

export function App() {
  return (
    <ThemeProvider>
      <ModeProvider>
        <Chrome />
      </ModeProvider>
    </ThemeProvider>
  );
}
