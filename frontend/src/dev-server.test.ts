// @vitest-environment node
//
// Node rather than jsdom: this imports the Vite config, and Vite's own internals
// assert on a `TextEncoder` that jsdom replaces with one producing a different
// `Uint8Array`. Nothing here touches a DOM.
/**
 * Which interface the dev server binds.
 *
 * A LAN-visible server has to be asked for — binding everything by default would
 * put an editorial pipeline on the network because somebody ran the dev script —
 * but asking for it must then *work*, and the failure mode this test exists for
 * was silent: the server came up on loopback, said so in one line nobody reads
 * as an error, and the machine across the room got connection refused.
 *
 * So the environment variable is honoured by the config itself rather than only
 * by a flag inside a shell script, which is what makes `HOST=0.0.0.0 npm run dev`
 * behave the same way as the script that wraps it.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

async function configuredHost(host?: string): Promise<string | boolean | undefined> {
  vi.resetModules();
  if (host === undefined) delete process.env.HOST;
  else process.env.HOST = host;

  const loaded = await import('../vite.config');
  const config = loaded.default as { server?: { host?: string | boolean } };
  return config.server?.host;
}

afterEach(() => {
  delete process.env.HOST;
});

describe('the dev server', () => {
  it('stays on this machine unless it is told otherwise', async () => {
    expect(await configuredHost()).toBe('127.0.0.1');
  });

  it('binds what HOST names, so the script and a bare `npm run dev` agree', async () => {
    expect(await configuredHost('0.0.0.0')).toBe('0.0.0.0');
  });

  it('accepts a specific interface, not only all or nothing', async () => {
    expect(await configuredHost('192.168.1.184')).toBe('192.168.1.184');
  });
});
