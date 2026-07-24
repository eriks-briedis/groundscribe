"""Execution provenance: how every artefact was actually produced (phase 03).

A distinct subsystem from the editorial artefacts of phase 02 and from
operational logs. Provenance here is structured domain data — typed rows with
foreign keys — not an unstructured event blob, so a reviewer can ask "which
prompt, which model call, which tool result, which decision produced this
paragraph?" and get an answer by query rather than by log grepping
(plan/00 → observable provenance is part of the product).

Growth
------

Provenance grows faster than the artefacts it explains — every retry stores
another request, every stage another timeline. plan/03 flags this as a risk and
defers *tuning* to phase 13, so what exists now are the places the tuning will
attach, chosen so that adding it later changes no caller:

- **Dedup is already in effect.** Payloads are content-addressed snapshots, and
  :meth:`ProvenanceRecorder._write_snapshot` serialises them canonically (sorted
  keys, no incidental whitespace) so two logically identical payloads hash
  identically. A repair attempt that resends the same request, or an accepted
  attempt whose raw and parsed forms coincide, costs one blob rather than two.
- **Compression attaches at the blob store.** Every provenance byte reaches disk
  through :class:`~groundscribe.storage.blob_store.BlobStore`, so a codec added
  there covers the whole subsystem without touching a single call site.
- **The trace stores references, never copies.** Trace-event payloads carry ids
  and outcomes; the record itself lives in its own typed row. The timeline
  therefore stays small, and phase 13 can age events out without losing any
  invocation, decision or artefact — only the ordering between them.
"""

from __future__ import annotations
