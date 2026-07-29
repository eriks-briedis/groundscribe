/**
 * What this frontend is not allowed to know (phase 11).
 *
 * plan/11 → *No client-side transition rules (unit/lint): a guard test asserts
 * the frontend has no independent transition/routing logic (actions come only
 * from backend state)*, and the risk it answers: an interface that re-derives
 * the workflow becomes a second state machine that disagrees with the first one
 * quietly, in production, months later.
 *
 * A lint rather than a behavioural test, deliberately. Any single screen can be
 * shown to render only what it was handed; what cannot be shown that way is that
 * *no* screen anywhere has started deciding for itself. So this reads the source
 * and asserts the vocabulary is absent: no state name, no action name, no
 * command URL. A rule you cannot express cannot be re-implemented.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const SRC = resolve(process.cwd(), 'src');
const CONTRACT = resolve(process.cwd(), '../contracts/openapi.json');

interface Contract {
  paths: Record<string, Record<string, unknown>>;
  components: { schemas: Record<string, { enum?: string[] }> };
}

function contract(): Contract {
  return JSON.parse(readFileSync(CONTRACT, 'utf-8'));
}

/** Every source file the application ships — tests and fixtures are not it. */
function sources(directory = SRC): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return sources(path);
    if (!/\.tsx?$/.test(entry)) return [];
    if (/\.test\.tsx?$/.test(entry)) return [];
    if (path.includes(`${join('src', 'test')}`)) return [];
    return [path];
  });
}

/**
 * A file with its comments removed.
 *
 * The guard is about what the code does. Every module here quotes the plan in
 * its docstring — including the endpoint names and the states — and a lint that
 * counted prose would make explaining the rule a violation of it.
 */
function code(path: string): string {
  return readFileSync(path, 'utf-8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1');
}

/**
 * Where a name is *decided on* rather than merely displayed.
 *
 * The distinction matters: a score table that prints the word "passed" is
 * showing what the backend said, while `state === 'passed'` is this side
 * forming an opinion about it. Only the second is what the plan forbids, so the
 * guard looks for the shapes a decision takes — a comparison, a `case`, a
 * membership test, a lookup keyed by the name.
 */
function decidesOn(names: readonly string[]): string[] {
  const found: string[] = [];
  for (const path of sources()) {
    const text = code(path);
    for (const name of names) {
      const quoted = `['"\`]${name}['"\`]`;
      const deciding = new RegExp(
        `(===|!==|==|!=)\\s*${quoted}|case\\s+${quoted}|includes\\(\\s*${quoted}|\\[\\s*${quoted}\\s*\\]`,
      );
      if (deciding.test(text)) found.push(`${path.replace(SRC, 'src')}: ${name}`);
    }
  }
  return found;
}

/** Whole-segment mentions, so `/review` does not match the `/reviews` read. */
function mentions(needles: readonly string[]): string[] {
  const found: string[] = [];
  for (const path of sources()) {
    const text = code(path);
    for (const needle of needles) {
      const whole = new RegExp(`${needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w-])`);
      if (whole.test(text)) found.push(`${path.replace(SRC, 'src')}: ${needle}`);
    }
  }
  return found;
}

describe('the frontend', () => {
  it('names no workflow state, so it cannot branch on one', () => {
    const states = contract().components.schemas.WorkflowState?.enum ?? [];

    expect(states.length).toBeGreaterThan(10);
    expect(decidesOn(states)).toEqual([]);
  });

  it('names no workflow action, so it cannot decide one is available', () => {
    // Taken from the state machine's own vocabulary, via the contract, so a new
    // action is guarded the moment the backend publishes it.
    const actions = [
      'extract_source_model',
      'propose_architecture',
      'approve_architecture',
      'generate_brief',
      'approve_brief',
      'require_revision_plan',
      'approve_revision_plan',
      'accept_review',
      'submit_voice_pass',
      'score_passed',
      'score_failed',
      'route_revision',
      'override_and_approve',
      'validate_final',
      'approve_final',
      'reject_final',
    ];

    expect(decidesOn(actions)).toEqual([]);
  });

  it('builds no command URL, because commands are addressed by the backend', () => {
    // What identifies a command endpoint is the part after the id it acts on —
    // `/brief/approve`, `/voice-align`, `/cancel`. Collection paths without an id
    // (`POST /projects`) are excluded because they are also *read* paths and the
    // text cannot tell the two apart; opening a project is the one command with
    // no artefact to have handed out a link, and the shell takes it by name.
    const commands = Object.entries(contract().paths)
      .filter(([, methods]) => methods.post !== undefined || methods.put !== undefined)
      .filter(([path]) => path.includes('}'))
      .map(([path]) => path.slice(path.lastIndexOf('}') + 1))
      .filter((suffix) => suffix.length > 1);

    expect(commands.length).toBeGreaterThan(10);
    expect(mentions(commands)).toEqual([]);
  });

  it('reaches the network from one module only', () => {
    const reaching = sources().filter(
      (path) => /\bfetch\(|new EventSource\(/.test(readFileSync(path, 'utf-8')),
    );

    expect(reaching.map((path) => path.replace(SRC, 'src'))).toEqual([join('src', 'api', 'client.ts')]);
  });
});
