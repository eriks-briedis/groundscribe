/**
 * A backend, faked at the boundary the app actually talks to (phase 11).
 *
 * `fetch` is stubbed rather than the client module, deliberately: a test that
 * mocked `fetchDashboard` would prove a component renders whatever it is handed,
 * which is never in doubt. Stubbing the transport means the generated client
 * really runs — the URL is built, the query is serialised, the response is
 * parsed — so a screen that asks for the wrong path fails here rather than in a
 * browser.
 *
 * Every recorded request is kept, because half of what these tests assert is
 * *what the app asked for*: an action bar that posts the wrong command, or a
 * read that quietly writes, is visible only in the request log.
 */
import { vi } from 'vitest';

export interface RecordedRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
}

export interface FakeBackend {
  requests: RecordedRequest[];
  /** Requests that were not plain reads — the evidence a screen acted. */
  commands: RecordedRequest[];
}

type Route = unknown | ((request: RecordedRequest) => unknown);

/**
 * Answer `routes` (keyed by path, without the `/api` prefix) and record the rest.
 *
 * An unrouted path answers 404 rather than hanging or returning undefined: a
 * screen fetching something nobody stubbed should fail loudly and say which URL.
 */
export function fakeBackend(routes: Record<string, Route>): FakeBackend {
  const backend: FakeBackend = { requests: [], commands: [] };

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      // The generated client sends a `Request`; `sendCommand` sends a URL and an
      // init. Both arrive here, so both are unpacked.
      const sent = input instanceof Request ? input : null;
      const url = new URL(sent ? sent.url : String(input), 'http://localhost');
      const path = url.pathname.replace(/^\/api/, '');
      const method = (sent?.method ?? init?.method ?? 'GET').toUpperCase();
      const sentBody = sent ? await sent.clone().text() : undefined;
      const rawBody = sentBody || (init?.body ? String(init.body) : '');
      const request: RecordedRequest = {
        method,
        path,
        query: url.searchParams,
        body: rawBody ? JSON.parse(rawBody) : undefined,
      };
      backend.requests.push(request);
      if (method !== 'GET') backend.commands.push(request);

      const route = routes[path];
      if (route === undefined) {
        return new Response(JSON.stringify({ detail: `no route for ${path}` }), {
          status: 404,
          headers: { 'content-type': 'application/json' },
        });
      }
      const payload = typeof route === 'function' ? (route as (r: RecordedRequest) => unknown)(request) : route;
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );

  return backend;
}

/** One frame a fake stream can deliver. */
export interface StreamFrame {
  event: string;
  data: Record<string, unknown>;
}

export interface FakeStream {
  /** Deliver a frame to whoever is listening, as the backend would. */
  emit: (frame: StreamFrame) => void;
  /** Whether the component closed the stream when it went away. */
  closed: () => boolean;
  url: () => string;
}

/**
 * Stand in for `EventSource`, which jsdom does not implement.
 *
 * Kept as a class the app constructs, rather than a mocked module, so the test
 * also observes the two things that go wrong with streams in practice: listening
 * for the wrong event name, and never closing the connection.
 */
export function fakeEventSource(): FakeStream {
  const listeners = new Map<string, Set<EventListener>>();
  const state = { closed: false, url: '' };

  class StubEventSource {
    constructor(url: string) {
      state.url = url;
    }
    addEventListener(name: string, listener: EventListener): void {
      const existing = listeners.get(name) ?? new Set<EventListener>();
      existing.add(listener);
      listeners.set(name, existing);
    }
    removeEventListener(name: string, listener: EventListener): void {
      listeners.get(name)?.delete(listener);
    }
    close(): void {
      state.closed = true;
    }
  }

  vi.stubGlobal('EventSource', StubEventSource);

  return {
    emit: ({ event, data }) => {
      const message = new MessageEvent(event, { data: JSON.stringify(data) });
      for (const listener of listeners.get(event) ?? []) listener(message);
    },
    closed: () => state.closed,
    url: () => state.url,
  };
}
