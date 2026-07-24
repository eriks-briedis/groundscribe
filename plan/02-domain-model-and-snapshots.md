# Phase 02 — Domain Model & Content-Addressed Snapshots

## Goal

Define the core editorial domain entities and the immutable, content-addressed snapshot
store with branching lineage. This is the persistent backbone every later stage writes
to. Nothing here calls a model — it is pure data modelling and invariants.

## Depends on

- Phase 01 (DB harness, migrations, test tooling).

## Spec references

- *Domain model → Core editorial entities*.
- *Product principles → Immutable snapshots over destructive edits*.
- *Product principles → Structured outputs where decisions matter*.
- *Domain model → ArtifactSnapshot*, *Version lineage*.
- *Source-of-truth extraction → Claims and evidence* (claim classification enum).
- *Storage → Artefact storage* (content addressing, dedup).

## Deliverables

Pydantic schemas + SQLAlchemy models + Alembic migrations for the editorial entities:

- `User`, `Project`, `SourceDocument`, `SourceSegment`, `SourceClaim`, `SourceGap`,
  `UserAnswer`, `ContentArchitecture`, `ArticleConcept`, `ArticleBrief`, `Article`,
  `ArticleVersion`, `Review`, `ReviewIssue`, `RevisionPlan`, `VoiceProfile`,
  `ValidationReport`.
- `ArtifactSnapshot` store: `id, artifact_type, schema_version, content_hash,
  content_location, size, created_by_execution_id, parent_snapshot_id`, plus a
  content-addressed blob store abstraction (filesystem-backed for local dev) with dedup.
- Claim classification enum: directly-supported fact, user observation, interpretation,
  hypothesis, opinion, unknown, unsupported claim.
- Lineage support on branching artefacts (source models, architectures, briefs,
  article versions, voice profiles, reviews, validation reports): `parent`, `children`,
  `branch_status`, `selection_status`.

## Test-first specification

- **Snapshot immutability (unit):** once written, a snapshot's content cannot be mutated;
  attempting to overwrite creates a new snapshot with a new hash and a `parent_snapshot_id`
  link.
- **Hash-mutation detection (provenance/unit):** tampering with stored content is
  detectable because the recomputed hash no longer matches `content_hash`.
- **Content-addressed dedup (unit):** storing identical content twice yields one blob and
  two references (same `content_hash`, same `content_location`).
- **Lineage branching (unit):** a single parent can have multiple children (e.g. two
  rewrites from one draft); each child records its parent; querying children returns both
  branches.
- **Claim classification (unit):** every `SourceClaim` carries exactly one classification
  from the enum and retains references to originating `SourceSegment`s.
- **Schema/DB parity (unit):** each Pydantic schema round-trips to/from its SQLAlchemy row
  without loss; `schema_version` is recorded.
- **Migration test:** new tables `upgrade`/`downgrade` cleanly.

## Implementation tasks

1. Write Pydantic schemas for each editorial entity, including enums and provenance
   reference fields.
2. Implement the content-addressed blob store (hash function, write-once, dedup, integrity
   check).
3. Implement `ArtifactSnapshot` + lineage fields and helpers (`fork_from`, `children_of`).
4. Map entities to SQLAlchemy models; generate Alembic migration.
5. Enforce immutability at the store boundary (no in-place update path for snapshotted
   artefacts).
6. Make all Test-first specs green.

## Exit criteria / spec-conformance checklist

- [ ] All 17 editorial entities modelled as Pydantic + SQLAlchemy with `schema_version`.
- [ ] `ArtifactSnapshot` is content-addressed, write-once, deduplicated.
- [ ] Immutability enforced; supersession always creates a new snapshot with parent link.
- [ ] Hash mismatch detects accidental mutation.
- [ ] Lineage supports multiple children per parent (branching), with branch/selection
      status.
- [ ] Claim classification enum complete and each claim links to source passages.
- [ ] Migrations round-trip.

## Risks & non-goals for this phase

- **Non-goal:** provenance execution entities (phase 03), any stage logic, retrieval.
- **Non-goal:** `created_by_execution_id` is a nullable FK now; the invariant "every
  artefact references a creating execution" is enforced in phase 05 once executions exist.
- **Risk:** premature retrieval/embedding fields — defer to phase 12/spec "Search and
  retrieval".
