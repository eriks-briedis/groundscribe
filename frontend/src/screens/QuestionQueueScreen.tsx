/**
 * The question queue (phase 11).
 *
 * plan/11 → *blocking + high-value questions, the reason each matters, answer
 * status, unknown/confidential options, resulting source-model changes*.
 *
 * Two things this screen refuses to do. It does not decide which questions are
 * worth asking — the backend surfaced them and says which block the run — and it
 * does not treat "I don't know" as a non-answer. `unknown` and `confidential`
 * are answers the source model records and reasons about, so they are offered as
 * plainly as a sentence is.
 */
import { useState } from 'react';

import { sendCommand, type QuestionQueue, type QuestionView } from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { fetchQuestions } from '@/api/client';

/** How an answer may be given. The backend's vocabulary, not a UI invention. */
const RESPONSES = [
  { value: 'answered', label: 'answered' },
  { value: 'unknown', label: "unknown — I don't know" },
  { value: 'confidential', label: 'confidential — cannot be published' },
] as const;

export interface QuestionQueueScreenProps {
  projectId: string;
  actor: string;
}

export function QuestionQueueScreen({ projectId, actor }: QuestionQueueScreenProps) {
  const resource = useResource<QuestionQueue>(() => fetchQuestions(projectId), [projectId]);

  return (
    <Loaded resource={resource}>
      {(queue) => (
        <section className="screen screen--questions">
          <h1>Questions</h1>
          {(queue.questions ?? []).map((question) => (
            <Question
              key={question.id}
              question={question}
              actor={actor}
              onAnswered={() => resource.reload()}
            />
          ))}
        </section>
      )}
    </Loaded>
  );
}

interface QuestionProps {
  question: QuestionView;
  actor: string;
  onAnswered: () => void;
}

function Question({ question, actor, onAnswered }: QuestionProps) {
  const [text, setText] = useState('');
  const [response, setResponse] = useState<string>(RESPONSES[0].value);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const answer = async () => {
    if (!question.answer_path) return;
    setBusy(true);
    setProblem(null);
    try {
      await sendCommand(question.answer_path, { text, answered_by: actor, response });
      onAnswered();
    } catch (error) {
      setProblem(String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="card" data-testid={`question-${question.id}`}>
      <h2>{question.question}</h2>
      <p>
        <span className="tag">{question.priority}</span> {question.why_it_matters}
      </p>
      {question.description ? <p className="muted">{question.description}</p> : null}

      {question.answer ? (
        <div className="answer">
          <p>
            <strong>{question.answer.response_type}</strong>: {question.answer.text}
          </p>
          <p className="muted">answered by {question.answer.answered_by}</p>
          {question.answer.diff_snapshot_id ? (
            <p className="muted">rebuilt the source model ({question.answer.diff_snapshot_id})</p>
          ) : null}
        </div>
      ) : (
        <form
          className="answer-form"
          onSubmit={(event) => {
            event.preventDefault();
            void answer();
          }}
        >
          <label>
            Your answer
            <textarea value={text} onChange={(event) => setText(event.target.value)} />
          </label>
          <label>
            How you are answering
            <select value={response} onChange={(event) => setResponse(event.target.value)}>
              {RESPONSES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy || !question.answer_path}>
            answer
          </button>
          {problem ? <p role="alert">{problem}</p> : null}
        </form>
      )}
    </article>
  );
}
