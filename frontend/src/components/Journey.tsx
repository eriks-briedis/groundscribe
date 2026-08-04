/**
 * Where the work has got to (phase 11).
 *
 * plan/11 → the interface *displays backend state*. Every fact on screen here is
 * one the backend published: the phases and their order, which of them is
 * current, the sentence describing what is happening, and whether the run is
 * waiting on a person or on the pipeline. This file arranges them and adds
 * nothing — there is no list of stages in it to fall out of date.
 *
 * The strip answers the question a person actually arrives with, which is not
 * "what state is the run in" but "is it my move?". So the whose-turn pill is the
 * loudest thing in the card, the state's own sentence is the headline, and the
 * machine's vocabulary (`source_questions_required`) appears nowhere.
 */
import type { Dashboard } from '@/api/client';

/**
 * Optional at runtime, whatever the contract says.
 *
 * The generated type marks it required, and a *current* backend always sends it.
 * A backend one restart behind does not, and the first version of this file
 * turned that into a blank screen with a stack trace — a page that lost the
 * artefacts it did have because one field it did not need was missing. Absence
 * is reported here, not filled in and not fatal.
 */
type Journey = Dashboard['journey'] | undefined;

/** How the backend names who is holding things up, in words for a person. */
const WAITING_LABEL: Record<string, string> = {
  you: 'Your move',
  pipeline: 'Working',
  nobody: 'Finished',
};

export function JourneyStrip({ journey }: { journey: Journey }) {
  const steps = journey?.steps ?? [];
  const waiting = journey?.waiting_on ?? 'pipeline';

  if (steps.length === 0) return null;

  return (
    <ol className="journey__steps" data-testid="journey">
      {steps.map((step) => (
        <li
          key={step.id}
          className="journey__step"
          data-status={step.status}
          data-waiting={step.status === 'current' ? waiting : undefined}
          data-testid={`phase-${step.id}`}
          // The status is carried in an attribute for style and spoken for
          // everyone else; a bar whose only meaning is its colour says nothing
          // to a screen reader and nothing in greyscale.
          title={step.blurb}
        >
          <span className="journey__rule" />
          <span className="journey__label">
            {step.title}
            <span className="visually-hidden"> — {step.status}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

export interface NowProps {
  journey: Journey;
  /** Rendered inside the card: the commands this state offers. */
  children?: React.ReactNode;
}

/**
 * The one card a person reads before anything else: what is happening now.
 *
 * It leads with the sentence rather than the phase name because the sentence is
 * the part that differs between "reading the source" and "waiting for you" —
 * two situations that share a phase and could not be more different to the
 * person looking at them.
 */
export function NowCard({ journey, children }: NowProps) {
  const waiting = journey?.waiting_on ?? 'pipeline';
  const current = (journey?.steps ?? []).find((step) => step.status === 'current');

  return (
    <section className="now" data-waiting={journey ? waiting : undefined} data-testid="now">
      {journey ? (
        <>
          <div className="now__phase">
            <span className={`pill pill--${waiting}`} data-testid="waiting-on">
              {WAITING_LABEL[waiting] ?? waiting}
            </span>
            {current ? <span className="eyebrow">{current.title}</span> : null}
          </div>
          <p className="now__headline" data-testid="headline">
            {journey.headline}
          </p>
          {current ? <p className="now__blurb">{current.blurb}</p> : null}
        </>
      ) : (
        // Said plainly rather than papered over: the commands below are still
        // the backend's own, and still safe to press.
        <p className="muted">
          This backend did not report where the run has got to. It is probably running an older
          build — restart it and this fills in.
        </p>
      )}
      {children}
    </section>
  );
}
