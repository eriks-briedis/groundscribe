# Phase 13 — Security, Privacy & Export

## Goal

Harden confidentiality end-to-end and implement export: enforce confidentiality flags at
final validation and export, guarantee secrets never reach logs/prompts/artefacts/traces,
implement trace-retention modes + encryption at rest + project-level trace export/deletion,
and produce clean export formats plus a sanitised execution report.

## Depends on

- Phase 03 (redaction hook, trace records), Phase 08 (final validation), Phase 09/11
  (export endpoints + UI), Phase 06 (confidentiality flags on segments/claims).

## Spec references

- *Security and privacy* (local-first storage, provider visibility, secret management,
  confidentiality controls, trace-retention modes, encryption and trace export).
- *Human approval and export* (export formats, sanitised execution report).
- *Non-goals* (never store secrets in traces).
- *Observability* (correlated structured logs).
- *Risks → Confidential information in traces; Trace-storage growth*.

## Deliverables

- **Confidentiality flags** on source claims/segments: publishable, internal, confidential,
  excluded-from-model-input, excluded-from-final-output, excluded-from-exported-traces —
  enforced at final validation (phase 08) and at export.
- **Confidentiality-aware request construction:** material flagged excluded-from-input is
  never sent to a provider; provider-visibility rules honoured per project.
- **Provider visibility surface:** which provider/model receives the source, local vs
  external, whether confidential sections exist, what content is sent, what is preserved in
  the trace (rendered in phase-11 UI, data provided here).
- **Secret management:** env vars in dev, OS keychain for packaged desktop, encrypted
  storage for hosted; keys never written to logs/prompts/artefacts/traces (redaction before
  persistence, from phase 03, extended + audited here).
- **Trace-retention modes:** full / redacted-full / metadata-and-structured-only /
  no-raw-provider-payloads / temporary-raw-retention / minimal-operational-logging;
  local-first may default to detailed retention but the choice is explicit.
- **Encryption at rest** for sensitive trace content; separate secret storage; expiration
  policies for raw provider payloads.
- **Project-level trace export + deletion**; **sanitised trace export** with warnings before
  exporting confidential material; sanitised execution report for debugging/portfolio.
- **Export formats:** Markdown, plain text, HTML, clipboard-ready; export uses the version
  that passed validation and matches the recorded content hash.
- **Trace-storage controls:** compression, dedup (content addressing from phase 02),
  retention policies, storage-use reporting.

## Test-first specification

- **Confidential blocked from output (unit):** material flagged confidential /
  excluded-from-output cannot appear in publishable/exported article; final validation
  fails if it does.
- **Excluded-from-input never sent (unit/provenance):** flagged-excluded material is absent
  from the effective request sent to any provider.
- **Redaction end-to-end (provenance):** an injected secret/confidential token is absent
  from every persisted record and every export, while the record still exists.
- **Retention-mode filtering (unit):** each retention mode persists exactly the permitted
  record classes (e.g. metadata-only mode stores no raw provider payloads).
- **Excluded-from-trace export (provenance):** sanitised/restricted trace exports omit
  trace-excluded content and warn before exporting any confidential material.
- **Encryption at rest (unit):** sensitive trace content is encrypted on disk; secrets are
  stored separately from trace data.
- **Export fidelity (unit):** Markdown/plain-text/HTML/clipboard exports preserve the
  passed version's content and match its content hash.
- **Provider-visibility data (unit):** the visibility surface reports provider, local/
  external, presence of confidential sections, sent content, and trace-preserved content.

## Implementation tasks

1. Extend confidentiality flags + enforce at final validation and export.
2. Implement confidentiality-aware request construction + provider-visibility data.
3. Audit + extend redaction; add encryption at rest + separate secret storage +
   payload-expiration policies.
4. Implement trace-retention modes + storage-use reporting + retention/compression.
5. Implement project-level trace export/deletion + sanitised export with warnings.
6. Implement export formats + sanitised execution report.
7. Make all tests green.

## Exit criteria / spec-conformance checklist

- [ ] Confidentiality flags enforced at final validation and export.
- [ ] Excluded-from-input material never reaches a provider.
- [ ] Secrets never appear in logs/prompts/artefacts/traces (audited).
- [ ] All trace-retention modes implemented and explicit.
- [ ] Encryption at rest + separate secret storage + raw-payload expiration.
- [ ] Project-level trace export/deletion + sanitised export with pre-export warnings.
- [ ] Export formats produced from the passed version with matching content hash.
- [ ] Provider-visibility surface data available to the UI.

## Risks & non-goals for this phase

- **Non-goal:** multi-tenant SaaS security model (explicit product non-goal).
- **Risk:** confidential info surviving in traces — the primary risk this phase closes;
  test redaction-before-persistence end-to-end.
