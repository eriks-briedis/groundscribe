# Phase 10 — Personal Voice System

## Goal

Implement the persistent personal voice system that replaces generic humanisation:
structured voice profiles, the hard-rule / strong-preference / tendency strength model,
the three-level precedence hierarchy, calibration onboarding, learning-from-edits with an
approval gate, and overfitting/repetition detection.

## Depends on

- Phase 07 (voice-alignment stage consumes profiles), Phase 02 (VoiceProfile entity),
  Phase 03 (interventions/provenance), Phase 09 (API/CLI to manage profiles).

## Spec references

- *Personal voice system* (voice profile, hard rules/strong preferences/tendencies, voice
  sources, calibration, learning from edits, voice hierarchy, avoiding overfitting).
- *Product principles → Human control at high-leverage decisions* (edit → permanent rule
  requires approval).

## Deliverables

- **VoiceProfile structure:** tone, language, structure, prohibited patterns, punctuation
  — each as specific operational instructions, not vague labels.
- **Instruction strength model:** hard rules (rarely violated), strong preferences
  (normally followed, justified exceptions allowed), tendencies (usual style, not
  mandatory templates). The voice-alignment stage (phase 07) consumes strengths
  accordingly.
- **Voice hierarchy + precedence:** global user profile < project profile < article
  override; resolver produces the effective instruction set and records the source +
  version of each active instruction.
- **Voice calibration (onboarding):** generate several short variants of the same passage
  (differing in depth/formality/directness/opinion/narrative); user marks what feels
  right/wrong; system proposes an initial editable profile.
- **Learning from edits:** detect recurring user edits (e.g. dramatic→concrete); do **not**
  auto-update the permanent profile — instead detect pattern → present inferred preference
  → show supporting examples → ask to make it a permanent rule → store only after approval.
  Manual edits record whether they are eligible as voice-training evidence.
- **Overfitting/repetition detection:** detect structural sameness across recent articles
  (identical openings, repeated section sequences, reused contrast patterns, similar
  conclusions, repeated rhetorical devices, repeated cadence) and warn, without forcing a
  single template.

## Test-first specification

- **Precedence resolution (unit):** article override beats project profile beats global;
  the effective instruction set records each instruction's source + version.
- **Strength enforcement (unit):** a hard rule (e.g. "no em dashes", "never use the
  internal product name") is enforced by the voice pass; a tendency is not applied as a
  mandatory template.
- **Calibration (unit):** variant generation covers the differing dimensions; user marks
  produce a proposed profile the user can edit before saving.
- **Learn-from-edits gate (unit/provenance):** a recurring edit pattern yields a
  *suggestion*, not an automatic profile change; the rule persists only after explicit
  approval; the manual edit records its voice-training-eligibility flag.
- **Overfitting detection (unit):** given recent articles sharing structure, the detector
  flags sameness; given varied articles, it does not.

## Implementation tasks

1. Model the VoiceProfile structure + strength levels (extend phase-02 entity).
2. Implement the precedence resolver with source/version tracking.
3. Wire strength semantics into the phase-07 voice-alignment stage.
4. Implement calibration variant generation + proposed-profile construction.
5. Implement edit-pattern detection + suggestion + approval-gated persistence.
6. Implement cross-article repetition/overfitting detection + warnings.
7. Expose profile management via the phase-09 service layer/API/CLI.
8. Make all tests green.

## Exit criteria / spec-conformance checklist

- [ ] Profiles hold specific operational instructions across all five categories.
- [ ] Hard-rule / strong-preference / tendency strengths modelled and honoured.
- [ ] Precedence (article > project > global) resolved with source + version shown.
- [ ] Calibration proposes an editable initial profile from user marks.
- [ ] Edit learning is approval-gated; nothing updates the permanent profile silently.
- [ ] Manual edits record voice-training eligibility.
- [ ] Overfitting/repetition detection flags structural sameness without forcing templates.

## Risks & non-goals for this phase

- **Non-goal:** voice-profile portability, fine-tuning from edits (future opportunities).
- **Risk:** style homogenisation — mitigated by tendencies-not-templates + repetition
  detection + article overrides (spec risk mitigation).
