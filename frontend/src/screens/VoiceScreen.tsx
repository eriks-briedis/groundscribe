/**
 * The voice editor (phase 16).
 *
 * `POST /voice/profiles` had existed since phase 10 and nothing called it. The
 * consequence was not that authors had a worse voice than they wanted — it was
 * that every project ran on an empty profile, because the only way to fill one
 * in was to write JSON at the API by hand, and nobody did. An endpoint with no
 * screen is a feature the product does not have.
 *
 * Three things this screen is careful about.
 *
 * **It shows what is in force, not what this author saved.** The rules reaching
 * the prose are the shipped ones plus anything saved at a narrower scope, and a
 * screen listing only the second half would make the first half invisible —
 * which is how an author ends up surprised by a rule they never wrote.
 *
 * **It seeds from what is already in force.** Starting an author on a blank list
 * is what produced the empty profile in the first place. The editor opens with
 * the rules already applying, so saving is an edit rather than an act of
 * authorship, and the first save keeps everything the shipped profile said
 * except what was deliberately changed.
 *
 * **It does not validate a hard rule itself.** A hard rule must name literal
 * strings it prohibits, and that is the document's rule, enforced by the
 * backend. The form surfaces the refusal rather than pre-empting it, because a
 * second copy of the rule here is a second place for it to drift.
 */
import { useState } from 'react';

import {
  ApiError,
  fetchEffectiveVoice,
  saveVoiceProfile,
  type EffectiveVoice,
  type VoiceInstruction,
} from '@/api/client';
import { Loaded, useResource } from '@/app/resource';

/** The backend's vocabulary, not a UI invention. */
const CATEGORIES = [
  'tone',
  'language',
  'structure',
  'prohibited_patterns',
  'punctuation',
] as const;

const STRENGTHS = [
  { value: 'hard_rule', label: 'Hard rule — never violated, and checked' },
  { value: 'strong_preference', label: 'Strong preference — followed unless there is a reason' },
  { value: 'tendency', label: 'Tendency — how I usually write' },
] as const;

export interface VoiceScreenProps {
  projectId: string;
  actor: string;
}

interface Draft {
  id: string;
  category: string;
  strength: string;
  text: string;
  prohibits: string;
}

const BLANK: Draft = {
  id: '',
  category: 'language',
  strength: 'strong_preference',
  text: '',
  prohibits: '',
};

/**
 * Where a saved profile applies.
 *
 * Global by default, because a voice is a property of the person writing rather
 * than of one publication — phase 10's calibration infers a global profile from
 * an author's own prose, and saving to the project by default would leave every
 * new project starting from the shipped rules again.
 *
 * Article scope exists in the resolver and is deliberately not offered here: it
 * is for one piece that has to differ, which is a decision made while looking at
 * that piece rather than at a list of rules.
 */
const SCOPES = [
  { value: 'global', label: 'Everything I write' },
  { value: 'project', label: 'This project only' },
] as const;

export function VoiceScreen({ projectId, actor }: VoiceScreenProps) {
  const resource = useResource<EffectiveVoice>(
    () => fetchEffectiveVoice(projectId),
    [projectId],
  );

  return (
    <Loaded resource={resource}>
      {(voice) => <VoiceEditor voice={voice} projectId={projectId} actor={actor} reload={resource.reload} />}
    </Loaded>
  );
}

function VoiceEditor({
  voice,
  projectId,
  actor,
  reload,
}: {
  voice: EffectiveVoice;
  projectId: string;
  actor: string;
  reload: () => void;
}) {
  const inForce = voice.active ?? [];
  const [draft, setDraft] = useState<Draft>(BLANK);
  const [added, setAdded] = useState<VoiceInstruction[]>([]);
  const [scope, setScope] = useState<'global' | 'project'>('global');
  const [problem, setProblem] = useState('');
  const [saved, setSaved] = useState('');

  function add() {
    setProblem('');
    if (!draft.id.trim() || !draft.text.trim()) {
      setProblem('A rule needs an id and something to say.');
      return;
    }
    const prohibits = draft.prohibits
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    setAdded([
      ...added,
      {
        id: draft.id.trim(),
        category: draft.category as VoiceInstruction['category'],
        strength: draft.strength as VoiceInstruction['strength'],
        text: draft.text.trim(),
        prohibits,
        rationale: '',
      },
    ]);
    setDraft(BLANK);
  }

  /**
   * Save the rules in force plus the new ones, as this project's voice.
   *
   * Seeded from what already applies rather than from the additions alone: a
   * profile carrying only the new rules would be a narrower scope that silently
   * kept everything wider, which is true but reads to the author as though the
   * others had been dropped. Saving what they can see is the behaviour that
   * matches the screen.
   */
  async function save() {
    setProblem('');
    setSaved('');
    const seeded: VoiceInstruction[] = inForce.map((active) => ({
      id: active.instruction_id,
      category: active.category as VoiceInstruction['category'],
      strength: active.strength as VoiceInstruction['strength'],
      text: active.text || active.instruction_id,
      prohibits: (active.prohibits ?? '')
        .split(',')
        .map((term) => term.trim())
        .filter(Boolean),
      rationale: active.rationale ?? '',
    }));
    const byId = new Map(seeded.map((item) => [item.id, item]));
    for (const item of added) byId.set(item.id, item);

    try {
      const version = await saveVoiceProfile(
        {
          schema_version: 1,
          name: actor,
          version: String(Date.now()),
          scope,
          description: 'Saved from the voice editor.',
          instructions: [...byId.values()],
          suppresses: [],
          first_person: true,
        },
        { userId: actor, projectId: scope === 'project' ? projectId : undefined },
      );
      setAdded([]);
      setSaved(
        `Saved as version ${version.version}, in force for ${
          scope === 'project' ? 'this project' : 'everything you write'
        }.`,
      );
      reload();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    }
  }

  return (
    <section className="screen screen--voice">
      <header className="screen__header">
        <h1>Voice</h1>
        <p className="screen__subtitle">
          The rules the prose is held to. Every article is written, edited and scored against
          these; a rule with nothing to check for is a preference, and the ones marked hard are
          checked against the finished text.
        </p>
      </header>

      <section className="panel" data-testid="voice-in-force">
        <h2>In force</h2>
        <p className="muted">{(voice.sources ?? []).join(' + ') || 'no profile'}</p>
        <ul className="rules">
          {inForce.map((rule) => (
            <li key={rule.instruction_id}>
              <span className="tag">{rule.strength.replace(/_/g, ' ')}</span>{' '}
              <span className="tag tag--muted">{rule.category.replace(/_/g, ' ')}</span>
              <p>{rule.text || rule.instruction_id}</p>
              {rule.prohibits ? <p className="muted">Never: {rule.prohibits}</p> : null}
              <p className="muted">
                {rule.instruction_id} · {rule.source}
                {rule.overrides ? ` · overrides ${rule.overrides}` : ''}
              </p>
            </li>
          ))}
        </ul>
        {inForce.length === 0 ? <p>No rules apply, which means nothing is being checked.</p> : null}
      </section>

      <section className="panel" data-testid="voice-add">
        <h2>Add a rule</h2>
        <label>
          Id
          <input
            value={draft.id}
            onChange={(event) => setDraft({ ...draft, id: event.target.value })}
            placeholder="no-em-dash"
          />
        </label>
        <label>
          What it says
          <textarea
            value={draft.text}
            onChange={(event) => setDraft({ ...draft, text: event.target.value })}
            rows={3}
          />
        </label>
        <label>
          Category
          <select
            value={draft.category}
            onChange={(event) => setDraft({ ...draft, category: event.target.value })}
          >
            {CATEGORIES.map((category) => (
              <option key={category} value={category}>
                {category.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </label>
        <label>
          How firmly it binds
          <select
            value={draft.strength}
            onChange={(event) => setDraft({ ...draft, strength: event.target.value })}
          >
            {STRENGTHS.map((strength) => (
              <option key={strength.value} value={strength.value}>
                {strength.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Never write these — one per line. A hard rule must name at least one.
          <textarea
            value={draft.prohibits}
            onChange={(event) => setDraft({ ...draft, prohibits: event.target.value })}
            rows={3}
          />
        </label>
        <button type="button" onClick={add}>
          Add
        </button>
      </section>

      {added.length > 0 ? (
        <section className="panel" data-testid="voice-pending">
          <h2>Not saved yet</h2>
          <ul className="rules">
            {added.map((rule) => (
              <li key={rule.id}>
                <span className="tag">{rule.strength.replace(/_/g, ' ')}</span> {rule.text}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {problem ? (
        <p className="failure" role="alert">
          {problem}
        </p>
      ) : null}
      {saved ? <p role="status">{saved}</p> : null}

      <label>
        Where these apply
        <select
          value={scope}
          onChange={(event) => setScope(event.target.value as 'global' | 'project')}
        >
          {SCOPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      <button type="button" onClick={save} disabled={added.length === 0}>
        Save voice
      </button>
    </section>
  );
}
