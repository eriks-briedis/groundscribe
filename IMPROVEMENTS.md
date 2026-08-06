# Improvements

Things the product should do and does not yet — each judged worth building,
none of them defects. Defects go in `KNOWN-ISSUES.md`; the difference is that
everything here works as designed and the design is what should change.

Each entry says what it costs and what it risks, because an improvement with
neither written down is a wish.

---

## 1. Tell the pipeline how many articles you want

**Status:** open. **Wanted by:** the author, after a run proposed more articles
than they intended to write. **Cost:** one field, one prompt paragraph, one
guard, one migration. **Risk:** medium — see below.

The architecture stage decides how many articles a source becomes. The author
can merge, split, remove and reorder afterwards on the architecture board, but
only after the proposal has been generated: the model spends a call deciding
something the author already knew the answer to, and the correction is manual
surgery on a document rather than a constraint that was in force from the start.

This is the same shape as `target_length_words`, which the product already
treats correctly. That is set by the author on the project, rendered into the
prompt, and the brief stage *refuses* a target that disagrees with it — with the
reasoning that length is the author's to set and not the model's. How many
articles a source becomes is the same kind of decision. It is about what the
author is willing to write and publish, not about what the source contains. The
model can advise; it should not decide.

### Shape

`max_articles: int | None` on `EditorialConstraints`, mirroring
`target_length_words`: nullable, and null means "you decide". Rendered into the
`propose_content_architecture` prompt, and enforced by a guard on the proposal
rather than left as an instruction — a limit only a prompt knows is a limit that
holds until the model changes, which is a lesson this repository has learned
five times over (see §2).

**A maximum, not an exact count.** "At most two" lets the proposal return one
when the source only supports one. "Exactly two" would have it manufacture a
second article out of thin material, which is the failure the product exists to
prevent.

### The part that makes it safe

**The proposal has to report what the cap excluded.** If the author asks for one
article and the source genuinely supports three, a silent cap means they never
learn the other two existed — the constraint would trade the model's
over-eagerness for the author's ignorance, which is a worse deal than the one it
replaced.

`competing_theses` is not this. That field holds alternative angles on one
article; what is needed is "these are the articles I did not propose, because
you asked for fewer". Either a new field on `ArchitectureProposal` or an honest
widening of an existing one.

### The risk, stated plainly

A cap can produce a worse article. Three articles' worth of material forced into
one is overstuffed, and it will fail `scope_discipline` at scoring — the author
will have made the decision early and paid for it late.

Two things make that acceptable. The proposal says what it left out, so the
decision is informed. And `approve_and_continue` already lets an author come
back for another of the approved concepts after publishing the first, so capping
at one defers the others rather than losing them.

---

## 2. Check that every guard-validated field is described in its prompt

**Status:** open. **Found:** five times in one afternoon, one job at a time.
**Cost:** a test and a small registry. **Risk:** low.

A field that a guard validates and no prompt describes is a field the model
fills by guessing, and the guess holds right up until the model behind it
changes. Every instance below was found by a real run failing, hours apart:

| field | checked by | described in its prompt |
| --- | --- | --- |
| `suggested_route` | routing | no |
| `source_ref` | `check_findings` | no |
| `claims_that_must_not_change` | `check_plan` | no |
| claim id continuity | `check_continuity` | no |
| `dimensions` | the provider's strict schema | n/a |

All four of the first are now described. The improvement is the check that would
have found them on the day they were written: for each stage, assert that every
field its guards name appears in the current version of its prompt.

It cannot be fully automatic — a guard names a Python attribute and a prompt is
prose — so it wants a small declared map from stage to the field names its
guards enforce, which is itself worth having as documentation.

---

## 3. `review_substantively` should define the five routes it asks for

**Status:** open. **Cost:** one prompt version. **Risk:** none.

The sixth instance of §2, unfixed because it does not block anything today. The
review prompt lists `factual_gap`, `architecture_issue`, `substantive_issue`,
`style_issue` and `minor_local` by name and defines none of them — exactly what
`score_article` did before it was fixed.

It has not caused a wrong route because review findings drive the revision plan
rather than the routing table; only a failing *score* routes on category. It
will matter the first time a reviewer's category is used for anything else, and
it is a twenty-line fix.

Observed on a live run: three of five findings came back `factual_gap` for
passages where the article asserted *more* than the source supported — which is
`substantive_issue`, and is the precise confusion the scoring prompt now names
outright.

---

## 4. Deciding the last finding should start the plan

**Status:** open. **Cost:** two lines. **Risk:** low.

Every other action a person takes advances the run afterwards — approving a
brief queues the draft, because asking someone to press *draft* immediately
after accepting a brief is asking them to confirm a decision they just made.

`decide_finding` does not, so accepting the last finding leaves the run parked in
`revision_plan_required` with everything it needs and nothing queued. The author
has to come back and press plan.

It was left out deliberately while the command was new — it moves the run
nowhere by design, and advancing from it is a second behaviour that wanted its
own thought. The thought: advancing is right when the decision was the last one
outstanding, and wrong while findings remain undecided, which `startable`
already knows how to ask.

---

## 5. Voice learning from edits is designed and unbuilt

**Status:** open. **Cost:** unclear — the hard part is deciding what an edit
teaches. **Risk:** low to build, high to build badly.

The source material describes a voice profile learned from the author's own
before-and-after edits. The profile itself is real: four scopes, categorised
instructions carrying strengths, applied by the voice pass and scored against.
The learning is not. `VoiceLearning.record_edit` exists and has no callers, so
nothing ever observes an author changing a sentence and concludes anything from
it.

Worth naming as unbuilt rather than quietly dropping, because it is the part of
the description a reader would find most interesting, and an article that
implied it worked would be wrong.

The reason to be careful: a system that learns from edits without being told
which edits were about *voice* will learn from corrections that were about
facts, and produce a profile that encodes an accident.

---

## 6. `reopen_architecture` is an edge with no way to take it

**Status:** open. **Cost:** one command and one endpoint. **Risk:** low.

The transition table permits `reopen_architecture` from `architecture_approved`
and from `stalled`, and the interface offers it — `reads.py` lists it among the
actions a person may take. There is no service method and no endpoint, so it is
an action the product advertises and cannot perform.

The same shape as `route_revision` before it was wired, and as
`decide_finding` before it was: an edge in the table that nothing outside the
tests could reach. Both of those turned out to matter more than they looked.

---

## 7. The most dangerous controls should not be the ones styled to be pressed

**Status:** open. **Found:** phase 16, looking at a run in progress. **Cost:** a
distinction in the action bar. **Risk:** none.

Observed on a project whose architecture proposal was still being generated. The
action bar offered six controls, and the two rendered as primary buttons were
`Abandon proposal` and `Cancel` — the only two that destroy something. Everything
safe was greyed out, because it belonged to the pipeline.

That is the styling rule working exactly as written and producing the opposite
of what it is for. `ActionBar` renders `taken_by === 'you'` as primary, which is
a good rule when a person's action is the one thing the run is waiting for, and
a bad one when a person's only available actions are the ways to stop it.

An action bar wants three weights, not two: what the run is waiting for you to
do, what you *may* do, and what ends the run. Cancelling should never look like
the next step, and it currently looks exactly like it.

Related to `KNOWN-ISSUES.md` §7, which is why `Abandon proposal` was offered
there at all — but separate from it. Even with that fixed, `Cancel` alone would
still be the only primary button on the screen for most of a run.

---

## 8. A Prettier configuration

**Status:** open. **Cost:** one file. **Risk:** none.

The frontend is written with single quotes throughout and there is no
`.prettierrc`, so running Prettier reformats every file it touches to its own
defaults — 285 lines of noise in one accidental invocation, none of it intended.

A committed config makes the house style enforceable instead of conventional.

---

## 9. Splitting a source strips the grounding out of every piece of it

**Status:** open. **Found:** phase 16, by reading an article the pipeline passed.
**Cost:** one rubric line and one prompt clause for the first two levers; the
third is §1. **Risk:** medium — a floor that is too high fails honest articles.

The architecture decides how many articles a source supports and which claims go
to each. It splits by *topic*, and evidence follows topic — so the article whose
subject is positioning gets the positioning claims and none of the artefacts,
because the artefacts belong to the topics that were split away from it.

Measured on a real run. A 92-claim source became five articles; the positioning
article was allocated **14 claims**, and the draft's own `omitted` field records
what the split sent elsewhere:

- detailed implementation status — reserved for a separate status article
- local-first storage, provider routing, retention, encryption — a separate
  provenance article
- scoring dimensions, weights, thresholds and configured numbers — a separate
  scoring article
- stack, packaging, database parity, framework and interface detail

Every concrete thing in the source was routed out of the article arguing that
concreteness is the product. What was left could only be conceptual, and the
result reads as fluent and un-grounded. (68 of the 92 claims reached any article
at all; 24 reached none.)

### The pipeline detected it and passed the article anyway

The score's lowest substantive dimension was `evidence_and_specificity` at 86,
with the deduction: *"names categories of traceable material but does not show a
concrete inspected artefact, field, decision record, review finding, score entry,
or source-linked explanation."* That is the defect, stated exactly. Overall 92.85,
passed.

It passed because the rubric's floors are `factual_fidelity` 90,
`thesis_and_focus` 80, `scope_discipline` 80 and `voice_adherence` 75.
`evidence_and_specificity`, `reader_value` and `structure_and_coherence` have
none. The conjunction protects against publishing something *wrong*; nothing
protects against publishing something *empty*, and an article can score well by
being accurate about how little it says.

### Three levers, in the order they are worth pulling

**A brief that requires a worked example** — *done*. The brief is a contract the
scorer checks, and a requirement it states outright is marked `rubric_required`,
which fails the article whatever it scored. `generate_article_brief` v2 adds a
conditional mandatory criterion: an article whose thesis is about how something
*works* must show it working once, on a real case from the source. Conditional
because an article whose thesis is a position or a report owes no worked example,
and requiring one everywhere buys padding.

**Per-content-type floors on the dimensions that have none** — replaces "a floor
on `evidence_and_specificity`", which was the wrong instrument and is worth
recording as such.

A single global floor cannot do this job. To fail the measured article at 86 it
would have to sit at 87–88, above the floors on focus (80), scope (80) and voice
(75) and just under factual fidelity (90) — which says specificity is nearly as
non-negotiable as accuracy, and that is not true of every article. The rubric
already knows this and expresses it through *weights*: an overview weights
evidence 0.05 and a deep dive 0.25, with the note that "an overview citing every
number would be a deep dive that failed to notice". `weights` is per content
type; `minimums` is global, and the two disagree.

So the change is to make `PassingPolicy.minimums` per content type as `weights`
already is, and only then to give evidence a floor that means something in each.
That is a schema change to the rubric and its config, not one line — which is why
it is filed here rather than done.

**A cap on how many articles a source becomes** — §1. It bounds how thin the
slicing gets, but on its own it produces one broad article rather than one
grounded one. It is the third lever and not the first.

### What this does not fix

The same run showed four other faults that all originate in the brief rather than
the draft: the disclaimer section placed last so the article ends on what it is
not claiming, two sections both restating the thesis, `should` used where the
reader needs `does`, and single-sentence paragraphs used often enough to stop
landing. The brief is generated, so these are brief-prompt questions, and they
are not addressed by any of the three levers above.

---

## 10. An advisor should draft the triage, not leave it to five cold decisions

**Status:** open, noted only — not designed. **Wanted by:** the author, during
the run of 2026-08-06, after triaging two consecutive reviews by hand. **Cost:**
unestimated. **Risk:** high, and the risk is the whole point — see below.

Triage is the pipeline's slowest human step and its least assisted one. A review
returns findings; each one has to be read, held against the draft, and decided
accept / reject / accept-with-an-edit, one at a time, with nothing on the screen
saying which are worth the round. The author's words: *slow, difficult and
clumsy*.

The measured cost on that run: two triage passes, ten findings. Of the ten,
**one** changed the article. Every other decision existed to say "no" — and the
second review's five findings were all `optional`, all reporting that the prior
score's complaints no longer held, i.e. five deliberate rejections to record
that there was nothing to do. That is the shape of the complaint: the work does
not scale with the decisions that matter, it scales with the findings returned.

The asymmetry is that the machine already knows most of what the author is
reconstructing by hand. The finding carries its severity, its category, its
confidence, its passage, and its `source_ref`; the score that preceded it
carries the failing conditions and the deductions; the plan that follows it
already reasons about which findings to combine, defer and reject — the planner
on this run deferred an accepted finding with a better rationale than the
acceptance had. All of that is available *before* the author is asked, and none
of it is offered to them.

So: an advisor that reads the findings against the score and the draft and
proposes a triage — a recommended decision per finding, with its reason and the
round cost — leaving the author to confirm, override, or work only the ones it
flagged as genuinely theirs.

### Why this is filed rather than built

The triage is the one step in the pipeline that exists specifically to be human.
Every other human pause approves work the machine did; this one is the machine
asking to be overruled. An advisor that is usually right is the most effective
way yet devised to stop someone reading the one finding where it is wrong — and
the failure is silent, because a confirmed recommendation and a considered
decision are the same row in the ledger afterwards.

Which means the design question is not "can a model rank findings" — it plainly
can — but what the author's confirmation has to *be* for it to still mean
something. At least: the advice arrives as a proposal the author can see the
reasoning of, the ledger records that a decision was advised and whether it was
changed, and a blocking finding never carries a pre-filled answer. That last one
matters most: it is exactly the finding an advisor would be most confident
about, and exactly the one where being overruled is the point.

Worth noting that the pattern would not be new — `propose_content_architecture`
already advises a decision that remains the author's, and the architecture board
exists so the advice can be edited before it binds. The difference is that a
proposed architecture is one document read as a whole; a triage is ten small
answers, and ten small confirmations is where attention actually goes.

---

## 11. A claim that costs a deletion should not cost a round

**Status:** open. **Wanted by:** the author, during the run of 2026-08-06, after
watching three revision rounds make an article worse. **Cost:** one route, one
prompt, one guard on what it may touch. **Risk:** medium — see below.

`unsupported_claims` is a publication condition, and correctly so: an article
that states as fact something the source does not support is not publishable,
whatever it scores. But the condition is binary and the remedy has one size. A
fabricated mechanism and a six-word rhetorical flourish fail the article
identically, and both are answered with a full substantive round.

### What that cost, measured

The run of 2026-08-06 ended its third round here:

| dimension | score | floor | |
|---|---|---|---|
| factual_fidelity | 96.0 | 90 | clear |
| thesis_and_focus | 93.0 | 80 | clear |
| scope_discipline | 93.0 | 80 | clear |
| structure_and_coherence | 90.0 | — | |
| reader_value | 90.0 | — | |
| evidence_and_specificity | 88.0 | — | |
| voice_adherence | 76.0 | 75 | clear by one |
| **overall** | **90.55** | **85** | clear by 5.55 |

Every threshold cleared. Eight deductions, seven `minor` and one `optional`,
none blocking. One publication condition failed it:

> `u001_draft_beats_blank_page` — "The draft beats the blank page."

Six words in the opening paragraph. The source supports that the repeated manual
workflow beat a single prompt; it does not support that a draft beats nothing.
The scorer is right. The fix is the delete key.

The round that produced this article ran 31 minutes, six model calls across
`review_substantively` (twice), `create_revision_plan`, `rewrite_substantively`,
`align_voice` and `score_article`, 315k input tokens, and ten triage decisions
by hand — to arrive at a draft failing on a sentence a person removes in four
seconds.

### The loop is anti-convergent for this article

Overall score by round: **91.75 → 92.05 → 91.1 → 90.55**. The high-water mark
was two rounds before the end. Each round removed the unsupported claim it was
sent back for and churned enough prose to earn fresh voice deductions — three of
the final eight were `voice_adherence`, which sat at 76 against a floor of 75.
One more round of the same trade would have failed the article on voice, having
been sent back for fidelity.

That is the part worth naming, because it is not a prompt defect. The rewrite is
doing what it was asked. A stage told to correct an argument rewrites paragraphs,
and rewritten paragraphs are re-voiced; the rubric charges for the churn under a
dimension nobody sent the article back for. So the loop trades a fixed cost in
voice for a variable gain in fidelity, and when the fidelity gain is one clause,
the trade is a loss.

### Shape

A route the score can take when its **only** failure is unsupported claims,
every dimension is above its floor, and no deduction is blocking — the case
where the article is finished and one or more sentences are not.

It goes to a stage that may only *remove or qualify*: given the claims and their
passages, it returns the same article with those passages cut or narrowed to what
the source says, and touches nothing else. One call, no plan, no triage, no voice
pass, and — this is the point — **no round**. Rounds are charged in `route()` for
a revision loop, and this is not one; it is the correction of a defect the score
has already localised to a span.

Guarded rather than instructed, on the lesson §2 records: a diff that touches
anything outside the named passages is refused, so "may only remove or qualify"
is a property of the output and not a sentence in a prompt. That guard is also
what makes skipping the voice pass safe — prose that only lost a clause has not
been re-voiced, so there is nothing for the voice stage to realign.

### Why it is a route and not a manual edit

Both should exist and the manual one is easier, but the manual one does not close
the loop. `manual_edits` records a person's change; it does not produce a version
the score can re-run against without a person driving each step. An article that
fails only on a removable clause should be able to finish without anybody being
asked — that is the same reasoning that makes approving a brief queue the draft.

### Risk

The condition it triggers on has to be narrow, because "cut the sentence" is the
wrong answer whenever the claim is load-bearing. An unsupported claim that the
thesis rests on cannot be deleted; removing it leaves an article arguing nothing,
and the honest outcome there is a `factual_gap` for the author to close with more
source material. The floors are the available proxy — a claim the argument needs
is one whose removal shows up in `thesis_and_focus` — but they are a proxy, and
the run that motivates this had a claim so peripheral that no dimension moved.

So the trigger should be conservative and say what it did: the route applies to a
score with no blocking deduction and every floor clear, it names the spans it
cut, and the re-score is the check. If the article comes back below a floor it
was above, the cut was load-bearing and the round was owed after all.
