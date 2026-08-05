/**
 * Editing the voice (phase 16, found missing while reading a bad article).
 *
 * `POST /voice/profiles` shipped in phase 10 and nothing called it, so every
 * project ran on whatever the resolver produced with no author input — which was
 * an empty document. The align-voice prompt then enforced nothing, the scorer
 * measured against nothing and returned 94, and validation prohibited nothing.
 * An endpoint with no screen is a feature the product does not have.
 *
 * The two properties worth pinning are about *seeding*, not about the form.
 * Opening on a blank list is what produced the empty profile, and saving only
 * the additions would silently drop the rules the author could see on screen.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { VoiceScreen } from './VoiceScreen';
import { fakeBackend } from '@/test/backend';
import { PROJECT_ID } from '@/test/fixtures';

const ACTOR = 'ada';

const IN_FORCE = {
  sources: ['shipped@1'],
  active: [
    {
      instruction_id: 'no-contrast-construction',
      category: 'prohibited_patterns',
      strength: 'strong_preference',
      text: 'State claims positively. Do not define a thing by what it is not.',
      rationale: 'Thirteen occurrences in one hundred and forty-two sentences.',
      prohibits: '',
      source: 'shipped@1 (global)',
      overrides: '',
    },
    {
      instruction_id: 'no-ai-filler',
      category: 'prohibited_patterns',
      strength: 'hard_rule',
      text: 'Cut the phrases that announce a point rather than making one.',
      rationale: '',
      prohibits: 'It is worth noting, delve into',
      source: 'shipped@1 (global)',
      overrides: '',
    },
  ],
  suppressed: [],
};

const SAVED = { id: 'v1', profile_id: 'p1', scope: 'global', version: '7', active: true };

describe('editing the voice', () => {
  it('shows what each rule says, not just its identifier', async () => {
    fakeBackend({ [`/projects/${PROJECT_ID}/voice`]: IN_FORCE });

    render(<VoiceScreen projectId={PROJECT_ID} actor={ACTOR} />);

    const panel = await screen.findByTestId('voice-in-force');
    expect(panel).toHaveTextContent('State claims positively');
    expect(panel).toHaveTextContent('It is worth noting');
    expect(panel).toHaveTextContent('shipped@1');
  });

  it('saves the rules already in force alongside the new one', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/voice`]: IN_FORCE,
      '/voice/profiles': SAVED,
    });

    render(<VoiceScreen projectId={PROJECT_ID} actor={ACTOR} />);
    await userEvent.type(await screen.findByLabelText(/^id$/i), 'no-em-dash');
    await userEvent.type(screen.getByLabelText(/what it says/i), 'Never use an em dash.');
    await userEvent.type(screen.getByLabelText(/never write these/i), '—');
    await userEvent.selectOptions(screen.getByLabelText(/how firmly/i), 'hard_rule');
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }));
    await userEvent.click(screen.getByRole('button', { name: /save voice/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    const saved = backend.commands[0]!;
    const body = saved.body as { instructions: { id: string }[] };
    const ids = body.instructions.map((item) => item.id);
    // The author's new rule *and* the two they were shown. Saving only the
    // addition would read, to the person who just looked at the list, as having
    // deleted the rest.
    expect(ids).toContain('no-em-dash');
    expect(ids).toContain('no-contrast-construction');
    expect(ids).toContain('no-ai-filler');
  });

  it('defaults to everything the author writes, not to this project', async () => {
    const backend = fakeBackend({
      [`/projects/${PROJECT_ID}/voice`]: IN_FORCE,
      '/voice/profiles': SAVED,
    });

    render(<VoiceScreen projectId={PROJECT_ID} actor={ACTOR} />);
    await userEvent.type(await screen.findByLabelText(/^id$/i), 'plain-words');
    await userEvent.type(screen.getByLabelText(/what it says/i), 'Short words.');
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }));
    await userEvent.click(screen.getByRole('button', { name: /save voice/i }));

    await waitFor(() => expect(backend.commands).toHaveLength(1));
    const saved = backend.commands[0]!;
    expect((saved.body as { scope: string }).scope).toBe('global');
    expect(saved.query.get('project_id')).toBeNull();
  });

  it('surfaces the backend refusing a hard rule that checks nothing', async () => {
    fakeBackend({
      [`/projects/${PROJECT_ID}/voice`]: IN_FORCE,
      '/voice/profiles': () =>
        new Response(JSON.stringify({ detail: 'hard rule "vague" names nothing it prohibits' }), {
          status: 422,
          headers: { 'content-type': 'application/json' },
        }),
    });

    render(<VoiceScreen projectId={PROJECT_ID} actor={ACTOR} />);
    await userEvent.type(await screen.findByLabelText(/^id$/i), 'vague');
    await userEvent.type(screen.getByLabelText(/what it says/i), 'Be good.');
    await userEvent.selectOptions(screen.getByLabelText(/how firmly/i), 'hard_rule');
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }));
    await userEvent.click(screen.getByRole('button', { name: /save voice/i }));

    // Reported rather than pre-empted: the rule lives in the document schema,
    // and a second copy in this form is a second place for it to drift.
    expect(await screen.findByRole('alert')).toHaveTextContent('names nothing it prohibits');
  });
});
