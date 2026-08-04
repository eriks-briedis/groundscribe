/**
 * The way in (phase 11).
 *
 * Every other screen addresses a project by id. This is where the first id comes
 * from, and where a new project comes from — the two things an artefact-first
 * interface still needs before any of its artefacts exist.
 *
 * The form asks for the bounds the project will publish under, because the
 * backend requires them and because they are the contract every later stage is
 * held to: a brief is written against a length and an audience, and validation
 * checks the finished article against the same ones. Asking later would mean
 * inventing them now.
 */
import { useState, type FormEvent } from 'react';

import { ApiError, fetchProjects, sendCommand, type ProjectIndex } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';

/** The depths phase 02 defines. Offered as a choice because it is one. */
const DEPTHS = ['overview', 'practitioner', 'deep_dive'] as const;

function readable(value: string): string {
  return value.replace(/_/g, ' ');
}

export interface HomeScreenProps {
  actor: string;
}

export function HomeScreen({ actor }: HomeScreenProps) {
  const resource = useResource<ProjectIndex>(() => fetchProjects(), []);

  return (
    <section className="screen screen--home">
      <header className="screen__header">
        <h1>Your projects</h1>
        <p className="screen__subtitle">
          Every project is one run of the pipeline: source in, checked article out, with the
          record of how it got there kept on this machine.
        </p>
      </header>

      <Loaded resource={resource}>
        {(index) => (
          <section className="panel">
            <h2>Open</h2>
            {index.projects?.length ? (
              <ul className="cards">
                {index.projects.map((project) => (
                  <li key={project.id} className="card" data-testid={`project-${project.id}`}>
                    <a className="card__title" href={`#/projects/${project.id}`}>
                      {project.title}
                    </a>
                    {project.description ? <p>{project.description}</p> : null}
                    <p className="muted">
                      <span className="tag">{readable(project.state)}</span> · {project.articles}{' '}
                      {project.articles === 1 ? 'article' : 'articles'} · opened{' '}
                      {project.opened_at.slice(0, 10)}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">Nothing here yet. Start one below.</p>
            )}
          </section>
        )}
      </Loaded>

      <NewProject actor={actor} onCreated={() => resource.reload()} />
    </section>
  );
}

function NewProject({ actor, onCreated }: { actor: string; onCreated: () => void }) {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [audience, setAudience] = useState('');
  const [platform, setPlatform] = useState('');
  const [depth, setDepth] = useState<string>('practitioner');
  const [words, setWords] = useState('1800');
  const [providers, setProviders] = useState('ollama');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const start = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const created = await sendCommand('/projects', {
        title,
        description,
        author_id: actor,
        constraints: {
          audience,
          platform,
          depth,
          target_length_words: Number(words) || null,
          allowed_providers: providers
            .split(/[,\s]+/)
            .map((name) => name.trim())
            .filter(Boolean),
          trace_retention_consent: true,
        },
      });
      onCreated();
      // Straight into it: leaving a new project to be found in a list is how the
      // missing entry point felt in the first place.
      window.location.hash = `#/projects/${created.project_id}`;
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="panel new-project form-grid" onSubmit={(event) => void start(event)}>
      <h2>Start a project</h2>
      <p className="muted">
        The bounds you set here are the contract every later stage is held to: the brief is
        written against them, and the finished article is checked against them.
      </p>

      <label htmlFor="title">Title</label>
      <input id="title" value={title} onChange={(event) => setTitle(event.target.value)} required />

      <label htmlFor="description">What it is about</label>
      <input
        id="description"
        value={description}
        onChange={(event) => setDescription(event.target.value)}
      />

      <label htmlFor="audience">Audience</label>
      <input
        id="audience"
        value={audience}
        onChange={(event) => setAudience(event.target.value)}
        placeholder="senior backend engineers"
      />

      <label htmlFor="platform">Platform</label>
      <input
        id="platform"
        value={platform}
        onChange={(event) => setPlatform(event.target.value)}
        placeholder="personal blog"
      />

      <label htmlFor="depth">Depth</label>
      <select id="depth" value={depth} onChange={(event) => setDepth(event.target.value)}>
        {DEPTHS.map((name) => (
          <option key={name} value={name}>
            {readable(name)}
          </option>
        ))}
      </select>

      <label htmlFor="words">Target length, in words</label>
      <input
        id="words"
        type="number"
        value={words}
        onChange={(event) => setWords(event.target.value)}
      />

      <label htmlFor="providers">Providers allowed to see the source</label>
      <input
        id="providers"
        value={providers}
        onChange={(event) => setProviders(event.target.value)}
      />

      <button type="submit" disabled={busy}>
        {busy ? 'Starting…' : 'Start the project'}
      </button>
      {problem ? (
        <p role="alert" className="failure">
          {problem}
        </p>
      ) : null}
    </form>
  );
}
