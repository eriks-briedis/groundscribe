/**
 * Summary first, payload on request (phase 11).
 *
 * plan/11's mitigation for trace overload: *summary views by default,
 * expandable raw payloads*. Everything heavy in this app — an effective request,
 * a raw response, a context selection, a stored document — is wrapped in one of
 * these, so a screen opens as a page a person can read rather than a wall of
 * JSON they have to scroll past.
 *
 * A button and a region rather than `<details>`, because the state is also read
 * by the debug mode: opening a stage inspector in debugging mode should expand
 * what an editorial reader would have left closed.
 */
import { useEffect, useState, type ReactNode } from 'react';

export interface DisclosureProps {
  summary: ReactNode;
  children: ReactNode;
  /** Open on first render — how debugging mode asks for everything at once. */
  open?: boolean;
}

export function Disclosure({ summary, children, open = false }: DisclosureProps) {
  const [expanded, setExpanded] = useState(open);

  // Follows the mode when it changes, without pinning the control: a person who
  // closed one panel in debugging mode keeps it closed until the mode moves again.
  useEffect(() => setExpanded(open), [open]);

  return (
    <div className="disclosure" data-expanded={String(expanded)}>
      <button
        type="button"
        className="disclosure__summary"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span aria-hidden="true">{expanded ? '▾' : '▸'}</span> {summary}
      </button>
      {expanded ? <div className="disclosure__body">{children}</div> : null}
    </div>
  );
}

export interface PayloadProps {
  value: unknown;
  label: ReactNode;
  open?: boolean;
}

/** A stored payload, shown as what it is: JSON when it parsed, text when it did not. */
export function Payload({ value, label, open = false }: PayloadProps) {
  if (value === null || value === undefined) {
    return (
      <p className="payload payload--empty">
        {label}: <span>nothing recorded</span>
      </p>
    );
  }
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return (
    <Disclosure summary={label} open={open}>
      <pre className="payload">{text}</pre>
    </Disclosure>
  );
}
