/**
 * The one way this application talks to the backend (phase 11).
 *
 * Everything below is generated-client plumbing. The types come from
 * `contracts/api-types.ts`, which is generated from `contracts/openapi.json`,
 * which is generated from the app — so a screen that asks for a field the
 * backend does not publish fails to compile.
 *
 * There are no rules in this file. It knows how to fetch and how to stream, and
 * nothing about what a state means or which action may follow which. That
 * knowledge has one home, in the backend's transition table, and plan/11 forbids
 * this side from forming a second opinion of it.
 */
import createClient from 'openapi-fetch';

import type { components, paths } from '@contracts/api-types';

/**
 * Where the API lives: same origin, under `/api`, which the dev server proxies
 * to the local backend.
 *
 * Absolute rather than relative because a request is built as a `Request`
 * object, and `new Request('/api/…')` has no base to resolve against outside a
 * document — which is exactly where the tests run.
 */
export const API_BASE = `${globalThis.location?.origin ?? 'http://localhost'}/api`;

export const api = createClient<paths>({
  baseUrl: API_BASE,
  // Deferred rather than captured: `createClient` binds `globalThis.fetch` at
  // construction, and this module is constructed at import — before a test (or a
  // browser extension, or a polyfill) has replaced it.
  fetch: (request) => globalThis.fetch(request),
});

/**
 * Every endpoint the application uses, as data.
 *
 * A map rather than a scatter of string literals, so "what does this app depend
 * on?" is answerable in one place — by a person, and by the contract test that
 * checks each one still exists.
 */
export const ENDPOINTS = {
  projectState: { path: '/projects/{project_id}', method: 'get' },
  dashboard: { path: '/projects/{project_id}/dashboard', method: 'get' },
  sourceWorkspace: { path: '/projects/{project_id}/source-workspace', method: 'get' },
  questions: { path: '/projects/{project_id}/questions', method: 'get' },
  architecture: { path: '/projects/{project_id}/architecture', method: 'get' },
  trace: { path: '/projects/{project_id}/trace', method: 'get' },
  articleWorkspace: { path: '/articles/{article_id}/workspace', method: 'get' },
  reviewHistory: { path: '/articles/{article_id}/reviews', method: 'get' },
  lineage: { path: '/articles/{article_id}/lineage', method: 'get' },
  inspectExecution: { path: '/executions/{execution_id}/inspect', method: 'get' },
  compareExecutions: { path: '/executions/compare', method: 'get' },
  effectiveVoice: { path: '/projects/{project_id}/voice', method: 'get' },
  job: { path: '/jobs/{job_id}', method: 'get' },
  jobEvents: { path: '/jobs/{job_id}/events', method: 'get' },
  session: { path: '/auth/session', method: 'get' },
} as const satisfies Record<string, { path: keyof paths; method: string }>;

export type Schemas = components['schemas'];
export type Dashboard = Schemas['ProjectDashboard'];
export type SourceWorkspace = Schemas['SourceWorkspace'];
export type QuestionQueue = Schemas['QuestionQueue'];
export type ArchitectureBoard = Schemas['ArchitectureBoard'];
export type ArticleWorkspace = Schemas['ArticleWorkspace'];
export type ReviewHistory = Schemas['ReviewHistory'];
export type LineageGraph = Schemas['LineageGraph'];
export type TraceView = Schemas['TraceView'];
export type TraceExecution = Schemas['TraceExecution'];
export type TraceFilter = Schemas['TraceFilter'];
export type StageInspection = Schemas['StageInspection'];
export type ExecutionComparison = Schemas['ExecutionComparison'];
export type CommandResponse = Schemas['CommandResponse'];
export type Job = Schemas['Job'];
export type FindingView = Schemas['FindingView'];
export type ScoreView = Schemas['ScoreView'];
export type DiffView = Schemas['DiffView'];
export type QuestionView = Schemas['QuestionView'];
export type ArticleCard = Schemas['ArticleCard'];
export type SessionState = Schemas['SessionState'];

/**
 * Told when the backend says the session is gone.
 *
 * A module-level listener rather than a parameter on every call: any request can
 * be the one that discovers a lapsed cookie, and threading that through ten
 * screens would mean ten places to forget it.
 */
type UnauthorizedListener = () => void;
let unauthorized: UnauthorizedListener = () => undefined;

export function onUnauthorized(listener: UnauthorizedListener): void {
  unauthorized = listener;
}

/** A request that failed, carrying whatever the backend said about it. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail || `request failed with ${status}`);
    this.name = 'ApiError';
  }
}

type Problem = { detail?: unknown };

/** Turn a client result into either the body or a thrown {@link ApiError}. */
function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined) return result.data;
  if (result.response.status === 401) unauthorized();
  const problem = result.error as Problem | undefined;
  throw new ApiError(result.response.status, String(problem?.detail ?? result.response.statusText));
}

export async function fetchProjectState(projectId: string): Promise<CommandResponse> {
  return unwrap(
    await api.GET('/projects/{project_id}', { params: { path: { project_id: projectId } } }),
  );
}

export async function fetchDashboard(projectId: string): Promise<Dashboard> {
  return unwrap(
    await api.GET('/projects/{project_id}/dashboard', {
      params: { path: { project_id: projectId } },
    }),
  );
}

export async function fetchSourceWorkspace(projectId: string): Promise<SourceWorkspace> {
  return unwrap(
    await api.GET('/projects/{project_id}/source-workspace', {
      params: { path: { project_id: projectId } },
    }),
  );
}

export async function fetchQuestions(projectId: string): Promise<QuestionQueue> {
  return unwrap(
    await api.GET('/projects/{project_id}/questions', {
      params: { path: { project_id: projectId } },
    }),
  );
}

export async function fetchArchitecture(projectId: string): Promise<ArchitectureBoard> {
  return unwrap(
    await api.GET('/projects/{project_id}/architecture', {
      params: { path: { project_id: projectId } },
    }),
  );
}

export async function fetchTrace(
  projectId: string,
  filters: readonly TraceFilter[] = [],
): Promise<TraceView> {
  return unwrap(
    await api.GET('/projects/{project_id}/trace', {
      params: { path: { project_id: projectId }, query: { filter: [...filters] } },
    }),
  );
}

export async function fetchArticleWorkspace(articleId: string): Promise<ArticleWorkspace> {
  return unwrap(
    await api.GET('/articles/{article_id}/workspace', {
      params: { path: { article_id: articleId } },
    }),
  );
}

export async function fetchReviewHistory(articleId: string): Promise<ReviewHistory> {
  return unwrap(
    await api.GET('/articles/{article_id}/reviews', {
      params: { path: { article_id: articleId } },
    }),
  );
}

export async function fetchLineage(articleId: string): Promise<LineageGraph> {
  return unwrap(
    await api.GET('/articles/{article_id}/lineage', {
      params: { path: { article_id: articleId } },
    }),
  );
}

export async function fetchInspection(executionId: string): Promise<StageInspection> {
  return unwrap(
    await api.GET('/executions/{execution_id}/inspect', {
      params: { path: { execution_id: executionId } },
    }),
  );
}

export async function fetchComparison(left: string, right: string): Promise<ExecutionComparison> {
  return unwrap(await api.GET('/executions/compare', { params: { query: { left, right } } }));
}

export async function fetchJob(jobId: string): Promise<Job> {
  return unwrap(await api.GET('/jobs/{job_id}', { params: { path: { job_id: jobId } } }));
}

/**
 * Send one command, by the path, method and body a screen was handed.
 *
 * Untyped in its body on purpose, and only here: an action bar is given
 * `available_actions` by the backend and posts what that action names. Typing
 * this against a union of the command endpoints would mean this side holding a
 * table of which action maps to which route — which is the transition table,
 * copied, in the place the plan says not to copy it.
 */
export async function sendCommand(
  path: string,
  body: Record<string, unknown> = {},
  method = 'POST',
): Promise<CommandResponse> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) unauthorized();
    throw new ApiError(response.status, String((payload as Problem)?.detail ?? ''));
  }
  return payload as CommandResponse;
}

/**
 * Whether this browser holds a session.
 *
 * Asked rather than inspected: the cookie is `HttpOnly`, so the only side that
 * can read it is the one that issued it.
 */
export async function fetchSession(): Promise<SessionState> {
  return unwrap(await api.GET('/auth/session'));
}

/** Exchange the shared password for a session cookie. */
export async function signIn(password: string): Promise<void> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (!response.ok) {
    const problem = (await response.json().catch(() => ({}))) as Problem;
    throw new ApiError(response.status, String(problem?.detail ?? 'sign-in failed'));
  }
}

/** Give the session back. */
export async function signOut(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: 'POST' });
}

/** One frame of a job's progress stream. */
export interface JobEvent {
  event: string;
  data: Record<string, unknown>;
}

/**
 * Watch a job until it finishes.
 *
 * `EventSource` rather than polling, because the backend already streams: phase
 * 09 resumes from a sequence number, so a reconnect continues rather than
 * replaying the run. Returns the unsubscribe the caller must run on unmount.
 */
export function subscribeToJob(jobId: string, onEvent: (event: JobEvent) => void): () => void {
  // Every browser has `EventSource`; jsdom does not, and neither does a
  // server-side render. Progress is an enhancement — the page reads the same
  // data over plain requests — so its absence must not take the screen down.
  if (typeof EventSource === 'undefined') return () => undefined;

  const source = new EventSource(`${API_BASE}/jobs/${jobId}/events`);
  const handle = (event: MessageEvent<string>) => {
    let data: Record<string, unknown> = {};
    try {
      data = JSON.parse(event.data) as Record<string, unknown>;
    } catch {
      data = { raw: event.data };
    }
    onEvent({ event: event.type, data });
  };

  // The backend names its frames (`event: job.status`), and an EventSource only
  // delivers named frames to a listener of that name — `onmessage` would see
  // none of them.
  for (const name of ['job.status', 'job.progress', 'message']) {
    source.addEventListener(name, handle as EventListener);
  }
  return () => source.close();
}
