/**
 * The generated client, and the contract it is generated from (phase 11).
 *
 * plan/11 → *TS types + client generated from OpenAPI*, and *a contract test
 * fails if the OpenAPI schema drifts from what the UI expects*.
 *
 * Two different kinds of drift, so two tests. The first catches a contract that
 * has moved on without the types being regenerated — a backend route added and
 * a stale `api-types.ts` left behind. The second catches the opposite: the UI
 * asking for an endpoint the backend no longer publishes. Type-checking cannot
 * see the second one, because the path a screen fetches is only a string until
 * the client resolves it against `paths`.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import openapiTS, { astToString } from 'openapi-typescript';
import { describe, expect, it } from 'vitest';

import { ENDPOINTS } from './client';

const CONTRACT = fileURLToPath(new URL('../../../contracts/openapi.json', import.meta.url));
const TYPES = fileURLToPath(new URL('../../../contracts/api-types.ts', import.meta.url));

function contract(): { paths: Record<string, Record<string, unknown>> } {
  return JSON.parse(readFileSync(CONTRACT, 'utf-8'));
}

describe('the generated client', () => {
  it('is generated from the committed contract, not from memory', async () => {
    const regenerated = astToString(await openapiTS(new URL(`file://${CONTRACT}`)));

    expect(readFileSync(TYPES, 'utf-8')).toBe(regenerated);
  });

  it('asks only for endpoints the contract declares', () => {
    const { paths } = contract();

    const missing = Object.values(ENDPOINTS).filter(
      ({ path, method }) => paths[path]?.[method] === undefined,
    );

    expect(missing).toEqual([]);
  });

  it('names one read per screen the plan describes', () => {
    // plan/11's core screens, by the read each of them opens with. Written out
    // here rather than derived from ENDPOINTS: a screen quietly dropped should
    // fail a test, and a list that read itself back never would.
    expect(Object.keys(ENDPOINTS)).toEqual(
      expect.arrayContaining([
        'projectState',
        'dashboard',
        'sourceWorkspace',
        'questions',
        'architecture',
        'articleWorkspace',
        'reviewHistory',
        'lineage',
        'trace',
        'inspectExecution',
        'compareExecutions',
        'job',
      ]),
    );
  });
});
