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
  type ActionLink,
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
  // Answers typed and not yet recorded, by question id. Held here so a sitting
  // is one request rather than one per answer.
  const [pending, setPending] = useState<Record<string, { text: string; response: string }>>({});

  const hold = (id: string) => (reply: { text: string; response: string } | undefined) =>
    setPending((current) => {
      const next = { ...current };
      if (reply) next[id] = reply;
      else delete next[id];
      return next;
    });

  return (
    <Loaded resource={resource}>
      {(queue) => {
        const questions = queue.questions ?? [];
        // Three groups, not two. Extraction finds more gaps than it asks about:
        // the policy caps how many are *surfaced* per round, because an author
        // faced with fifteen questions answers none. The rest are recorded and
        // answerable, and nothing waits on them — a screen that listed all of
        // them together would present the cap as though it had never applied.
        const open = questions.filter((question) => !question.resolved && question.surfaced);
        const unasked = questions.filter((question) => !question.resolved && !question.surfaced);
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
                  pending={pending[question.id]}
                  onPending={hold(question.id)}
                />
              ))}
            </div>

            <RecordAnswers
              record={queue.record}
              actor={actor}
              pending={pending}
              onRecorded={() => {
                setPending({});
                resource.reload();
              }}
            />

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
                      pending={pending[question.id]}
                      onPending={hold(question.id)}
                    />
                  ))}
                </div>
              </section>
            ) : null}

            {unasked.length ? (
              <section className="panel panel--secondary" data-testid="unasked">
                <h2>Also found, not asked</h2>
                <p className="muted">
                  {unasked.length} more {unasked.length === 1 ? 'gap' : 'gaps'} the extraction
                  noticed. Nothing is waiting on {unasked.length === 1 ? 'it' : 'them'} — the run
                  proceeds knowing what it does not know. Answer any that matter to you.
                </p>
                <div className="questions">
                  {unasked.map((question) => (
                    <Question
                      key={question.id}
                      question={question}
                      pending={pending[question.id]}
                      onPending={hold(question.id)}
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
  /** What the author has typed for this one and not yet recorded. */
  pending?: { text: string; response: string };
  onPending: (reply: { text: string; response: string } | undefined) => void;
}

function Question({ question, pending, onPending }: QuestionProps) {
  const [text, setText] = useState(pending?.text ?? '');
  const [response, setResponse] = useState<string>(pending?.response ?? RESPONSES[0].value);
  //: Whether the form is open over an answer already given. An author works
  //: down a queue and changes their mind halfway, and until the round is handed
  //: back nothing has read what they typed.
  const [editing, setEditing] = useState(false);

  // Held, not sent. Recording was priced per answer — a request, a stage
  // execution and a full reload of this screen each — and one run produced
  // eleven executions over eighteen minutes for a round of eleven questions.
  const answer = () => {
    onPending({ text, response });
    setEditing(false);
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
            answer();
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
              <button type="button" onClick={() => setEditing(false)}>
                Keep what I had
              </button>
            ) : null}
            {pending ? (
              <button type="button" onClick={() => onPending(undefined)}>
                Discard
              </button>
            ) : null}
            <button type="submit">
              {editing ? 'Save this instead' : pending ? 'Update answer' : 'Record answer'}
            </button>
          </div>
          {pending ? <p className="muted">Held. Record the round to send it.</p> : null}
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

interface RecordAnswersProps {
  record?: ActionLink | null;
  actor: string;
  pending: Record<string, { text: string; response: string }>;
  onRecorded: () => void;
}

/**
 * Recording a sitting's answers, once.
 *
 * Separate from handing the round back, and that separation is the point:
 * answering and submitting are different acts. An author may answer four now and
 * four tomorrow, and one control that did both would hand over whatever happened
 * to be typed so far — which is the reason `SUBMIT_ANSWERS` was deliberately kept
 * out of the action table in the first place.
 *
 * What changed is only the price of the first act. It used to be a request, a
 * stage execution and a full reload of this screen per answer; one run produced
 * eleven executions over eighteen minutes for a round of eleven questions.
 */
function RecordAnswers({ record, actor, pending, onRecorded }: RecordAnswersProps) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const held = Object.entries(pending);
  if (!record?.path || held.length === 0) return null;
  const path = record.path;

  const send = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await sendCommand(path, {
        answered_by: actor,
        answers: held.map(([gap_id, reply]) => ({
          gap_id,
          text: reply.text,
          response: reply.response,
        })),
      });
      onRecorded();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="record-answers" data-testid="record-answers">
      <button type="button" className="button--primary" disabled={busy} onClick={() => void send()}>
        {busy ? 'Recording…' : `Record ${held.length} ${held.length === 1 ? 'answer' : 'answers'}`}
      </button>
      {problem ? (
        <p role="alert" className="failure">
          {problem}
        </p>
      ) : null}
    </div>
  );
}
