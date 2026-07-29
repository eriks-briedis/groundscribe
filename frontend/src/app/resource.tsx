/**
 * Loading one thing from the backend (phase 11).
 *
 * Every screen has the same three states — loading, failed, loaded — and each of
 * them is a place an interface can quietly lie. A screen that renders its empty
 * shell while a request is in flight looks like a project with no articles; one
 * that swallows an error looks like a project with nothing in it at all.
 *
 * So the shape is fixed here, once: nothing is rendered until the answer is
 * known, a failure is shown in the backend's own words, and reloading is
 * explicit because a command has just changed what the screen is showing.
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react';

import { ApiError } from '@/api/client';

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

export function useResource<T>(load: () => Promise<T>, keys: readonly unknown[]): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  // `load` is rebuilt on every render by callers that close over an id; the keys
  // are what actually identifies the request, so they drive the effect.
  const run = useCallback(load, keys);

  useEffect(() => {
    let live = true;
    setLoading(true);
    run()
      .then((value) => {
        if (!live) return;
        setData(value);
        setError(null);
      })
      .catch((problem: unknown) => {
        if (!live) return;
        setError(problem instanceof ApiError ? problem.detail : String(problem));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [run, nonce]);

  return { data, error, loading, reload: () => setNonce((value) => value + 1) };
}

/** Render `children` once there is something to render, and say so until then. */
export function Loaded<T>({
  resource,
  children,
}: {
  resource: Resource<T>;
  children: (value: T) => ReactNode;
}) {
  if (resource.error !== null) {
    return (
      <p role="alert" className="failure">
        {resource.error}
      </p>
    );
  }
  if (resource.data === null) {
    return <p className="loading">Loading…</p>;
  }
  return <>{children(resource.data)}</>;
}
