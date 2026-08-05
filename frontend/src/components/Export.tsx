/**
 * Getting the article out (phase 16, found missing while auditing the API).
 *
 * `GET /versions/{id}/export` shipped in phase 13 with four formats and nothing
 * calling it, so a finished article lived only in the blob store: the pipeline
 * ran, validated, was approved, and there was no way to read the result outside
 * the application. The format enum's own docstring anticipated exactly this —
 * *"an option nobody can name in the UI is an option nobody uses"* — and was
 * true of all four.
 *
 * **Addressed by version.** What a person exports is the version that passed
 * validation, and the backend refuses to let that be implied: naming the version
 * is what makes exporting the wrong one impossible rather than merely unlikely.
 *
 * **The provenance travels with the bytes.** A Markdown file on a desktop cannot
 * say which run produced it, so the version id and content hash are shown beside
 * the content and written into the downloaded filename.
 */
import { useState } from 'react';

import {
  ApiError,
  exportVersion,
  type ExportFormat,
  type ExportedArticle,
} from '@/api/client';

/** The backend's four, in the order a person is likely to want them. */
const FORMATS: readonly { value: ExportFormat; label: string; extension: string }[] = [
  { value: 'markdown', label: 'Markdown', extension: 'md' },
  { value: 'html', label: 'HTML', extension: 'html' },
  { value: 'plain_text', label: 'Plain text', extension: 'txt' },
  { value: 'clipboard', label: 'For pasting', extension: 'txt' },
];

export interface ExportProps {
  versionId: string;
  /** Names the file, so a download is recognisable a month later. */
  title?: string;
}

export function Export({ versionId, title }: ExportProps) {
  const [format, setFormat] = useState<ExportFormat>('markdown');
  const [exported, setExported] = useState<ExportedArticle | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const [copied, setCopied] = useState(false);

  async function render() {
    setBusy(true);
    setProblem('');
    setCopied(false);
    try {
      setExported(await exportVersion(versionId, format));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!exported) return;
    await navigator.clipboard.writeText(exported.content);
    setCopied(true);
  }

  function download() {
    if (!exported) return;
    const chosen = FORMATS.find((item) => item.value === exported.format);
    const slug = (title ?? 'article').toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 60);
    // The version in the filename, because a file that cannot say which version
    // it holds is the provenance leaving with the bytes.
    const name = `${slug}-${exported.version_id.slice(0, 8)}.${chosen?.extension ?? 'txt'}`;
    const url = URL.createObjectURL(
      new Blob([exported.content], { type: exported.media_type }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="export" data-testid="export">
      <label>
        Format
        <select value={format} onChange={(event) => setFormat(event.target.value as ExportFormat)}>
          {FORMATS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <button type="button" onClick={render} disabled={busy}>
        {busy ? 'Rendering…' : 'Export this version'}
      </button>

      {problem ? (
        <p className="failure" role="alert">
          {problem}
        </p>
      ) : null}

      {exported ? (
        <>
          <p className="muted">
            {exported.format} · {exported.media_type} · version {exported.version_id} ·{' '}
            {exported.content_hash}
          </p>
          <div className="actions">
            <button type="button" onClick={download}>
              Download
            </button>
            <button type="button" onClick={copy}>
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <textarea
            className="export__content"
            readOnly
            rows={16}
            value={exported.content}
            aria-label="Exported article"
          />
        </>
      ) : null}
    </div>
  );
}
