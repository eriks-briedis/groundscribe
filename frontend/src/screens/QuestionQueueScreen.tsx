/**
 * The question queue (phase 11).
 *
 * plan/11 → *blocking + high-value questions, the reason each matters, answer
 * status, unknown/confidential options, resulting source-model changes*.
 *
 * This is an interview, not a form. The author works down the list answering
 * what they can — in whatever order the material comes back to them — and then
 * hands the round back in one go, which is the command that spends a model call
 * rebuilding the source model from everything they said.
 *
 * Three things this screen refuses to do. It does not decide which questions are
 * worth asking — the backend surfaced them and says which block the run. It does
 * not treat "I don't know" as a non-answer: `unknown` and `confidential` are
 * answers the source model records and reasons about, so they are offered as
 * plainly as a sentence is. And it does not decide when answering is possible:
 * a question the run will not take an answer for arrives without a path to post
 * one to, and this shows what it was given.
 */
import { useState } from 'react';

import {
  ApiError,
  sendCommand,
  type QuestionQueue,
  type QuestionView,
  type Schemas,
} from '@/api/client';
import { Loaded, useResource } from '@/app/resource';
import { fetchQuestions } from '@/api/client';

/** How an answer may be given. The backend's vocabulary, not a UI invention. */
const RESPONSES = [
  { value: 'answered', label: 'I can answer it' },
  { value: 'unknown', label: "I don't know" },
  { value: 'confidential', label: 'Confidential — record it, do not publish it' },
] as const;

export interface QuestionQueueScreenProps {
  projectId: string;
  actor: string;
}

export function QuestionQueueScreen({ projectId, actor }: QuestionQueueScreenProps) {
  const resource = useResource<QuestionQueue>(() => fetchQuestions(projectId), [projectId]);

  return (
    <Loaded resource={resource}>
      {(queue) => {
        const questions = queue.questions ?? [];
        const open = questions.filter((question) => !question.resolved);
        const answered = questions.filter((question) => question.resolved);

        return (
          <section className="screen screen--questions">
            <header className="screen__header">
              <h1>Questions</h1>
              <p className="screen__subtitle">
                What the source does not say. Answer as many as you can — nothing is rebuilt
                until you hand the round back.
              </p>
            </header>

            {questions.length === 0 ? (
              <p className="empty">
                No questions have been raised yet. They arrive with the source model.
              </p>
            ) : null}

            <div className="questions">
              {open.map((question) => (
                <Question
                  key={question.id}
                  question={question}
                  actor={actor}
                  onAnswered={() => resource.reload()}
                />
              ))}
            </div>

            <SubmitRound
              submit={queue.submit}
              actor={actor}
              answered={answered.length}
              open={open.length}
              onSubmitted={() => resource.reload()}
            />

            {answered.length ? (
              <section className="panel">
                <h2>Answered</h2>
                <div className="questions">
                  {answered.map((question) => (
                    <Question
                      key={question.id}
                      question={question}
                      actor={actor}
                      onAnswered={() => resource.reload()}
                    />
                  ))}
                </div>
              </section>
            ) : null}
          </section>
        );
      }}
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
  //: Whether the form is open over an answer already given. An author works
  //: down a queue and changes their mind halfway, and until the round is handed
  //: back nothing has read what they typed.
  const [editing, setEditing] = useState(false);

  const answer = async () => {
    if (!question.answer_path) return;
    setBusy(true);
    setProblem(null);
    try {
      await sendCommand(question.answer_path, { text, answered_by: actor, response });
      setEditing(false);
      onAnswered();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article
      className="question"
      data-testid={`question-${question.id}`}
      data-blocking={question.priority === 'blocking'}
      data-answered={Boolean(question.answer)}
    >
      <div className="question__meta">
        <span className={`tag${question.priority === 'blocking' ? ' tag--blocking' : ''}`}>
          {question.priority === 'blocking' ? 'blocks the run' : question.priority}
        </span>
        {question.group ? <span className="muted">{question.group}</span> : null}
      </div>
      <h2 className="question__ask">{question.question}</h2>
      {question.why_it_matters ? (
        <p className="question__why">{question.why_it_matters}</p>
      ) : null}
      {question.description ? <p className="muted">{question.description}</p> : null}

      {question.answer && !editing ? (
        <div className="answer">
          <p>
            <strong>{question.answer.response_type}</strong>
            {question.answer.text ? `: ${question.answer.text}` : ''}
          </p>
          <p className="muted">answered by {question.answer.answered_by}</p>
          {question.answer.diff_snapshot_id ? (
            <p className="muted">rebuilt the source model ({question.answer.diff_snapshot_id})</p>
          ) : null}
          {/* Only while the round is open. The backend withdraws the path once
              the rebuild has read the answer, because from there the source
              model was built from these words. */}
          {question.answer_path ? (
            <button
              type="button"
              onClick={() => {
                setText(question.answer?.text ?? '');
                setResponse(question.answer?.response_type ?? RESPONSES[0].value);
                setEditing(true);
              }}
            >
              Change this answer
            </button>
          ) : null}
        </div>
      ) : !question.answer_path ? (
        // No path, no form. The backend publishes one only while the run is
        // waiting for answers, so a form here would be a button whose failure
        // mode is a rejected request — and working out *why* it is closed is the
        // backend's job, not this screen's.
        <p className="empty">Closed to answers.</p>
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
          <div className="answer-form__controls">
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
            {editing ? (
              <button type="button" onClick={() => setEditing(false)} disabled={busy}>
                Keep what I had
              </button>
            ) : null}
            <button type="submit" disabled={busy}>
              {busy ? 'Recording…' : editing ? 'Save this instead' : 'Record answer'}
            </button>
          </div>
          {problem ? (
            <p role="alert" className="failure">
              {problem}
            </p>
          ) : null}
        </form>
      )}
    </article>
  );
}

interface SubmitRoundProps {
  submit: Schemas['ActionLink'] | null | undefined;
  actor: string;
  answered: number;
  open: number;
  onSubmitted: () => void;
}

/**
 * The end of the round: one command, however many answers went into it.
 *
 * Kept visible while the author works rather than appearing at the bottom, so
 * the cost of the next step — a rebuild, and a model call — is never a surprise
 * discovered after clicking. It is absent entirely when the backend did not
 * offer the command, which is how this screen knows the round is over.
 */
function SubmitRound({ submit, actor, answered, open, onSubmitted }: SubmitRoundProps) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  if (!submit?.path) return null;

  const handBack = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await sendCommand(submit.path as string, submit.requires_actor ? { actor_id: actor } : {});
      onSubmitted();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="round" data-testid="round">
      <span className="round__count">
        <strong>{answered}</strong> answered · <strong>{open}</strong> still open
      </span>
      <p className="round__note">
        Handing the round back rebuilds the source model from every answer on record. It costs one
        model call, and it is the only thing here that does.
      </p>
      <button
        type="button"
        className="button--primary"
        disabled={busy}
        onClick={() => void handBack()}
      >
        {busy ? 'Rebuilding…' : 'Rebuild with these answers'}
      </button>
      {problem ? (
        <p role="alert" className="failure">
          {problem}
        </p>
      ) : null}
    </div>
  );
}
