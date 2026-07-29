/**
 * The article, read as what it is (phase 11).
 *
 * plan/11's frontend deliverables include a *Markdown editor + preview*. This is
 * the preview half, and it is what the article workspace shows by default: the
 * prose was written as markdown, and a person judging whether it reads well
 * should see the headings and emphasis rather than the syntax.
 *
 * The source stays one click away. An artefact-first interface shows the
 * artefact as stored when asked — the rendered form is a convenience, and the
 * bytes are the thing the hash was taken over.
 *
 * The *editor* half is deliberately not here. No endpoint accepts a manually
 * edited version — phases 01–10 built none, and phase 10's manual-edit record is
 * written by the voice-learning module rather than by an API — so an editor
 * would be a text box with nowhere to save to. It arrives with the endpoint.
 */
import { marked } from 'marked';
import { useMemo } from 'react';

import { Disclosure } from './Disclosure';

export interface MarkdownProps {
  body: string;
  'data-testid'?: string;
}

export function Markdown({ body, ...rest }: MarkdownProps) {
  // `marked` is synchronous unless configured otherwise; the cast keeps that
  // explicit rather than rendering a promise as `[object Promise]`.
  const html = useMemo(() => marked.parse(body, { async: false }) as string, [body]);

  return (
    <div className="markdown">
      <article
        className="prose"
        data-testid={rest['data-testid'] ?? 'markdown'}
        // The content is the article this instance wrote and stored. It never
        // leaves the machine it was written on, and it has already been through
        // redaction; rendering it as markdown is the point of the component.
        dangerouslySetInnerHTML={{ __html: html }}
      />
      <Disclosure summary="markdown source">
        <pre className="payload">{body}</pre>
      </Disclosure>
    </div>
  );
}
