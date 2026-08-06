### GroundScribe — Technical Writer Pipeline

#### Product overview

GroundScribe is a local-first editorial workflow for turning technical source material into focused, accurate, publishable articles or article series.

It is built for technical builders who have useful material but do not want to repeat the same manual cycle of prompting, reviewing, rewriting, fact-checking, and style correction for every article.

The product is not primarily an article generator. Its value comes from managing the decisions around writing:

1. Establish what is factually supported by the source.
2. Identify information that is missing or unclear.
3. Decide whether the material supports one article or several.
4. Define a clear scope and thesis for each article.
5. Generate and revise drafts without drifting from the source.
6. Align the writing with the author's actual voice.
7. Evaluate whether the result is ready to publish.
8. Preserve a complete history of how the final result was produced.

The final publication decision always remains with the user.

This document has been revised after the system was run end to end on real source material. Sections now distinguish what is **built and observed** from what is **designed but unproven**, because the gap between the two turned out to be the most useful thing the first run produced.

---

#### Motivation

The product emerged from a repeated manual workflow.

A technical description would be given to a language model, which would generate an initial article. A separate review prompt would critique it. The article would then be rewritten, passed through a style or humanisation prompt, reviewed again, and revised until it appeared publishable.

This produced better results than a single prompt, but the process had several weaknesses.

##### Repetitive orchestration

The user had to move content between prompts, track the current draft, remember which feedback had already been addressed, and check whether later rewrites had introduced regressions.

The process worked, but it was difficult to manage consistently across multiple articles.

##### Factual drift

Every rewrite created another opportunity for the article to move away from the original technical source.

A model could:

- Add an unsupported implementation detail
- Remove an important qualification
- Turn an observation into a factual claim
- Replace precise technical language with broader but less accurate wording
- Generalise a personal lesson into an industry-wide conclusion

The system therefore needs an authoritative source model that remains separate from generated prose.

This was the correct diagnosis, and the first run confirmed it in a way the original description did not anticipate: **drift is not confined to the drafting and rewriting stages.** The revision *planner* — a stage that writes instructions rather than prose — invented two unsupported claims of its own while correcting one, because its replacement text was a factual assertion nobody had bound to the source. Any stage that dictates words the article will carry needs the source model, not only the stages named "draft" and "rewrite".

##### Weak scope control

Technical source material often contains several possible arguments.

A description of one project may include lessons about its architecture, failed approaches, data model, evaluation system, operational constraints, and product implications.

Putting everything into one article can produce an unfocused system summary. Splitting it too aggressively can create several thin and repetitive articles.

The pipeline needs to make an explicit editorial architecture decision before drafting.

##### Generic humanisation

Telling a model to make writing sound less AI-generated does not necessarily make it sound like the author.

Generic humanisation often introduces its own patterns:

- Forced informality
- Excessively short paragraphs
- Repeated rhetorical questions
- Artificial drama
- Predictable contrast structures
- Formulaic personal storytelling

The system needs to learn the user's actual preferences rather than apply a universal idea of natural writing.

##### Endless revision

A reviewer can always suggest another improvement.

Without clear thresholds and stopping conditions, the workflow can continue indefinitely. Later rewrites may make lateral changes rather than improve the article, and one quality dimension may improve while another deteriorates.

The pipeline therefore needs explicit scoring, routing, revision limits, and stagnation detection.

The first run showed this risk is real and that the caps alone do not address it. See *The revision loop can move backwards* below.

##### Opaque results

A weak article may be caused by the model, but it may also be caused by incomplete source extraction, missing context, a poor prompt, an invalid structured response, a repair attempt, a changed rubric, or a user override.

Preserving only the final article does not reveal which part of the pipeline failed.

The product therefore treats transparency as a core capability. Every result should be traceable back through the requests, responses, source material, decisions, scores, tool calls, and user interventions that produced it.

This is the assumption that paid off most. Every defect described in the next section was found by querying provenance, not by reading logs or re-running anything.

---

#### What the first full run proved

The system has been built through fourteen sequential phases and carries roughly 1,700 automated tests. It has been driven end to end on real source material, through source extraction, gap questions, architecture, brief, drafting, three substantive revision rounds, voice alignment, and five scoring passes.

The run has not yet reached final validation or export. Everything below is what the incomplete run has already established.

##### What worked

**An explicit state machine made every failure explainable.** Twenty-three states with a transition table held as data — not as branching code — meant that every surprising thing the pipeline did could be traced to a specific edge, actor, and guard. Several defects were diagnosed by reading the table rather than by instrumenting the run.

**Immutable versions with recorded linkage made defects provable rather than suspected.** The most damaging bug found — a review being handed the failures of a version it was not reading — was proved in a single query, because the score records which version it scored and the review records which version it read. Without that linkage it would have presented as "the reviewer is being unhelpful", which is the kind of complaint that never gets fixed.

**Guards beat instructions, every time the two were tested against each other.** The versioned prompt store declares each template's required variables and refuses to render without them. When a stage was given a new input, the declaration failed the render until the metadata was updated. That is a guard doing exactly its job. Every place where the same rule lived only in prompt text, it eventually stopped holding.

**Publication conditions independent of the score caught real fabrications.** An article can score well and still be unpublishable. Treating unsupported claims as a condition rather than a deduction meant a 92-point article was correctly refused. The scores alone would have shipped it.

**Deriving answers instead of restating them prevented a class of drift.** Which states are human pauses is computed from the edges' actors rather than kept as a second list, so the interface cannot disagree with the machine. The one place this reasoning had a blind spot is recorded below — but a derived answer with one known gap is still better than a duplicated list with unknown ones.

**Rerun and fork made prompt changes measurable.** A defective scoring prompt was proved by re-scoring the *identical* article version under a new prompt and comparing results, without moving the run. The ability to re-execute a stage against a fixed artefact turned "I think this prompt is better" into a before-and-after with one variable changed.

##### What did not

**The dominant defect had one shape, and it recurred seven times: a field that a guard validates and no prompt describes gets filled by guessing.** The schema requires a value, the model has no instruction telling it what belongs there, and it produces something structurally valid and semantically wrong. The system's own validation reports success. The clearest instance: the revision planner's prompt said "the source model settles factual disputes" about a document the stage had never been given. The planner settled them anyway, and its inventions passed every check downstream.

The lesson is not "write better prompts". It is that **schema and prompt are two halves of one contract and nothing in the toolchain checks that they agree.** A standing check that every schema field a stage may populate is described somewhere in that stage's prompt would have caught all seven.

**Asking for a list is not the same as asking for a complete list.** The scorer was asked for `unsupported_claims` and returned exactly one, three passes running, each time a different claim, each labelled `u001`. Every singleton cost a full revision round. Re-scoring the same article after the prompt was told the list is a complete finding rather than a worst example returned five claims at once. The model had been able to see all five the whole time.

**Scoping by run instead of by artefact is a bug that hides for a long time.** A helper selected "the newest score for this run" where it needed "the score of this version". That is the same answer until a rewrite creates a version nothing has scored — after which the reviewer is handed complaints about sentences that no longer exist. It parked a *passing* review at a human approval gate twice and made the author triage nine findings that all said "already fixed".

**The revision loop can move backwards.** Across three substantive rounds, the article's overall score went 91.75 → 92.05 → 91.1 → 90.55. The high-water mark was two rounds before the end. Each round removed the unsupported claim it was sent back for and churned enough prose to earn fresh style deductions; voice adherence finished at 76 against a floor of 75, having never once been the reason a round was opened. The loop was trading a fixed cost in voice for a variable gain in fidelity.

The final round cost 31 minutes, six model calls, 315,000 input tokens, and ten manual triage decisions — to arrive at an article failing on six words that a person deletes in four seconds. Every dimension was above its floor and no deduction was blocking. **A publication condition with no proportionality means a rhetorical flourish and a fabricated mechanism get the same remedy.**

**A state is not a step.** `revision_plan_required` covers the whole stretch between a review landing and a rewrite starting, and a person standing in it is being asked for one of two unrelated things: decide the findings, or approve the plan those decisions produced. The interface showed one headline for both, so an author with nine findings to triage was told to approve a plan that would not exist until they had. The machine's granularity is not the author's.

**Human triage does not scale with the decisions that matter — it scales with the findings returned.** Two triage passes, ten findings, one of which changed the article. Every other decision existed to record "no".

**Reversibility was withheld for no stated reason.** A finding could not be re-decided once decided, while a gap answer stays editable until the moment it is consumed. The same codebase held both rules. An accidental click could not be undone, and the only reason it cost nothing was luck.

**Telling a stage what it is for is not optional.** A review reached by routing a failed score was not told what the score had refused. It found nothing, the run advanced, and the score failed on the same claim three calls later — a loop bounded only by the rewrite cap, which it would have spent entirely without changing a word.

**Some fields exist and carry nothing.** Cost per model invocation is recorded on every call and is zero on every call. A provenance field that is present, typed, queryable, and empty is worse than an absent one, because dashboards will happily sum it.

---

#### Expenses

Figures below are read from the provenance record, not estimated. Both runs used a single hosted provider and one model throughout.

##### The run under discussion

Source ingestion through the fifth scoring pass, 07:40 to 10:38 — two hours fifty-eight minutes of wall clock, of which the author's own time was the triage steps.

| | |
|---|--:|
| Model calls | 27 |
| Tokens in | 1,276,409 |
| Tokens out | 195,028 |
| Substantive revision rounds | 3 of 3 |
| Article versions produced | 7 |
| Reviews | 6 |
| Revision plans | 2 |
| Scoring passes | 5 |
| Finding-triage decisions | 32 |
| User interventions recorded | 36 |

Thirty-two of the thirty-six recorded human actions were triage steps on individual review findings. They settled 29 findings: 20 rejected, 7 accepted, 2 edited. Two of the four triage passes produced a revision plan; the other two recorded, one finding at a time, that there was nothing to act on — and both of those were passes the author should never have been shown, because the reviewer had been handed the failures of a version it was not reading.

##### Where it went

| Stage | Calls | Tokens in | Tokens out |
|---|--:|--:|--:|
| Substantive review | 6 | 356,403 | 28,543 |
| Scoring | 5 | 311,734 | 40,874 |
| Source extraction | 3 | 135,981 | 71,138 |
| Substantive rewrite | 2 | 125,134 | 5,939 |
| Gap questions | 2 | 91,968 | 9,826 |
| Revision planning | 2 | 73,801 | 12,167 |
| Initial draft | 1 | 54,158 | 3,234 |
| Article brief | 1 | 48,275 | 2,813 |
| Content architecture | 1 | 46,325 | 5,654 |
| Voice alignment | 4 | 32,630 | 14,840 |

**Judging the article cost more than producing it.** Review and scoring together are 11 of 27 calls and 668,137 of 1,276,409 input tokens — 52% of everything sent. Writing the draft was 54,158 tokens in and 3,234 out, about 4% of input. The article was written once and judged eleven times.

That is not obviously wrong for a product whose thesis is that the decisions matter more than the generation. It is worth stating plainly anyway, because it sets what the optimisations are. Nothing here is made cheaper by a better drafting prompt. It is made cheaper by not sending an article back for a defect a deletion would fix, and by not asking a reviewer to re-read a draft it has already cleared.

**The last round in isolation:** 6 calls, 315,292 tokens in, 31,477 out, 31 minutes, and ten manual triage decisions — 25% of the run's entire input spend, to produce a version failing on six words.

##### Reliability, as a cost

Across both runs, 45 calls:

| Outcome | Calls |
|---|--:|
| Accepted first attempt | 38 |
| Accepted after a schema retry | 2 |
| Accepted after content repair | 1 |
| Returned invalid structured output | 4 |

Roughly one call in eleven needed a second attempt. Every retry is paid for twice — once for the rejected response and once for the replacement — and retries are recorded individually for exactly that reason. A prompt that validates on the first attempt is cheaper as well as more trustworthy.

##### The earlier run, for comparison

An earlier run on different material reached a passing state in 18 calls, 598,213 tokens in and 132,168 out, with no revision rounds charged and a single scoring pass at 92.85 with nothing outstanding.

The tempting reading is that the pipeline got worse. The more likely one is that it started looking properly. That run was scored under an earlier rubric prompt since demonstrated to report unsupported claims one at a time, and here it reported none. **A cheap run and a good run are not the same thing, and the cost record cannot tell them apart** — which is the argument for measuring findings-that-changed-the-article alongside tokens.

##### What is not recorded

Monetary cost is a field on every invocation and is zero on all 45 of them. Token counts, latency, provider, model, prompt version and retry type are all real. The currency figure is not, and anything summing it is summing nothing.

---

#### Core workflow

##### 1. Source ingestion

The workflow begins with technical source material such as:

- Markdown files
- Pasted notes
- Architecture descriptions
- Development retrospectives
- Existing drafts
- Technical documentation
- Obsidian notes
- Structured project summaries

The user also defines constraints such as:

- Intended audience
- Publishing platform
- Desired technical depth
- Approximate length
- Confidential information
- Internal names that must not be published
- Whether first-person narration is appropriate

The original source is preserved without modification.

##### 2. Source-of-truth extraction

The system converts the source material into a structured factual model.

It identifies:

- What was built
- What problem it addressed
- How it worked
- Important technical decisions
- Initial assumptions
- Failed approaches
- Changes made
- Reasons for those changes
- Remaining limitations
- Potential lessons or arguments

Claims are classified according to their evidential status:

- Directly supported fact
- User observation
- Interpretation
- Hypothesis
- Opinion
- Unknown
- Unsupported claim

Where possible, every claim remains linked to the source passage that supports it.

The source model is authoritative. Generated article text is not.

**Revised after the first run:** the source model must be given to *every* stage that produces text the article will carry, including stages whose output is instructions rather than prose. The rule is not "drafting stages get the source model". It is that any stage writing words the article will inherit is bound by the source exactly as the draft is.

##### 3. Gap analysis

The system identifies missing information that could affect the accuracy or usefulness of the article.

Questions are prioritised into three groups.

###### Blocking gaps

The article cannot be written truthfully without an answer.

###### High-value gaps

The article can proceed, but an answer would materially improve its specificity or credibility.

###### Optional enrichment

The answer might make the article richer but should not delay progress.

The system asks only blocking and selected high-value questions automatically.

For each question, the user can answer, skip, mark the information as unknown, mark it as confidential, defer it, or reject the premise.

This prevents the model from silently filling gaps with plausible prose — within the drafting stage. It does not prevent a later stage from doing the same thing, which is what happened, and is why the source model now reaches the planner.

##### 4. Content architecture

Once the source model is sufficiently complete, the system decides how the material should be structured.

Possible recommendations include:

- One focused article
- A multi-part series
- One main article with narrower follow-ups
- Several independent articles
- A short article with a deeper technical companion
- No article because the source lacks enough substance

The decision is based on more than source length.

The system considers:

- Number of distinct central arguments
- Strength of evidence for each argument
- Overlap between possible articles
- Whether each article can stand alone
- Reader knowledge requirements
- Publishing-platform constraints
- Whether one article would contain competing theses
- Whether splitting would create thin pieces

Each proposed article receives a thesis, audience, supporting evidence, explicit scope, exclusions, relationship to other articles, expected length, and confidence level.

**Open gap:** the stage decides how many articles the source becomes, and the author usually knows the answer already. A constraint set before the proposal would replace manual surgery on a generated document with a limit that was in force from the start.

##### 5. User architecture override

The architecture recommendation is advisory.

The user may:

- Merge article ideas
- Split an article
- Remove an article
- Change the order
- Rewrite a thesis
- Reassign evidence
- Change the audience
- Turn a series into one long article
- Reserve material for later

The system explains trade-offs but does not block a deliberate editorial decision.

Once approved, the architecture becomes a locked and versioned artefact. Later stages may recommend reopening it, but they cannot silently change it.

##### 6. Article brief

Each approved article receives a separate brief.

The brief defines:

- Central thesis
- Intended audience
- Reader question
- Argument structure
- Evidence assigned to each section
- Required examples
- Claims requiring qualification
- Target length
- Platform constraints
- Active voice rules
- Excluded material
- Material reserved for other articles
- Definition of done

The brief acts as a contract between the writer, reviewer, and rewriter.

**Observed tension:** the brief can require things the rubric then penalises. On the first run the brief demanded an inventory of provenance fields and a central contrast, and the scorer deducted for long lists and repeated contrast constructions. Neither component was wrong; nothing reconciles them. A contract that the grader has not read is only half a contract.

##### 7. Initial draft

The first draft is generated from the approved source model, architecture, brief, voice profile, and publishing constraints.

The drafting stage does not invent missing facts.

When information is unavailable, it omits the claim, qualifies it, inserts a visible unresolved marker, or returns the article to gap analysis.

Every draft is stored as a new immutable version.

##### 8. Substantive review

The first review focuses on argument and accuracy rather than sentence-level polish.

It evaluates:

- Factual fidelity
- Thesis clarity
- Focus
- Structure
- Evidence
- Specificity
- Scope discipline
- Reader value
- Repetition
- Technical accuracy
- Missing context
- Unsupported claims
- Quality of the conclusion

Each issue includes its severity, article location, evidence, recommended correction, and suggested pipeline route.

Issues are classified as:

- Blocking
- Major
- Minor
- Optional

The user may accept, reject, or edit individual findings. Reviewer output is evidence for revision, not an unquestionable instruction.

**Two rules learned here:**

A review reached by routing a failed score must be told what the score refused, and must be scoped to the version it is actually reading. Without the first, it looks for nothing and finds nothing. Without the second, it is handed complaints about a draft that no longer exists.

A review that disagrees with a score of the *same* version is a genuine conflict between two readings, and only a person settles it. A review of a version the score never saw is not a conflict at all, and stopping the author for one is a bug.

##### 9. Revision planning

Accepted feedback is converted into a coherent revision plan before the article is rewritten.

This stage exists because reviewer findings may be contradictory, optional, or harmful when applied independently.

The revision plan defines:

- Required changes
- Optional changes
- Sections to preserve
- Claims that must not change
- Sections to move or remove
- Whether the brief or architecture must be reopened

**Revised after the first run:** the plan's change descriptions are prose the rewrite will carry out, so they are factual assertions made on the article's behalf. The planner receives the source model and is instructed that where a finding says the draft claims more than the source supports, the safe correction is to cut or qualify — and the dangerous one is to substitute a different specific claim that reads better. If a change needs a fact the source does not contain, the plan says so instead of supplying it.

The durable version of this fix has not been built: each planned change should name the claim identifiers its replacement text rests on, so the plan checker can verify the binding mechanically instead of trusting the instruction.

##### 10. Substantive rewrite

The rewrite applies the approved revision plan.

It may change structure, section order, explanations, thesis wording, evidence placement, and scope.

It must not alter the source model or invent new facts.

Each rewrite creates another version linked to its parent.

##### 11. Voice alignment

Once the article is structurally sound, it receives a style-focused pass.

This replaces generic humanisation.

The stage may improve:

- Sentence rhythm
- Word choice
- Paragraph flow
- Formality
- Repetition
- Unnatural phrasing
- Excessive abstraction
- Common AI-writing patterns

It may not add new claims, examples, technical details, or structural changes.

If it discovers a structural problem, the article returns to substantive revision.

##### 12. Scoring and routing

The article is evaluated across seven dimensions:

| Dimension | Default weight | Floor |
|---|--:|--:|
| Factual fidelity | 25% | 90 |
| Thesis and focus | 15% | 80 |
| Structure and coherence | 15% | — |
| Evidence and specificity | 15% | — |
| Reader value | 10% | — |
| Scope discipline | 10% | 80 |
| Voice adherence | 10% | 75 |

Passing is a conjunction, not an average: the weighted overall must clear 85, every floored dimension must clear its floor, and no publication condition may be outstanding.

An article cannot pass if it contains a blocking issue, confidential information, or an unsupported major claim, even when its overall score is high.

Failures are routed to the stage that can actually correct them:

- Factual gap to source extraction or questions
- Architecture problem to architecture or brief
- Substantive problem to revision planning
- Style problem to voice alignment
- Minor local problem to a targeted correction

The system should not route every failure through another full rewrite.

**That last line was aspiration, and the first run showed it is not yet true where it matters most.** An unsupported claim always routes to a full substantive round regardless of how small it is. The missing route: when unsupported claims are a score's *only* failure, every dimension is above its floor, and no deduction is blocking, the article is finished apart from some sentences. That case should go to a stage that may only remove or qualify the named passages — one call, no plan, no triage, no voice pass, and no revision round charged, because the defect has already been localised to a span.

The guard matters more than the instruction: a diff touching anything outside the named passages is refused, which is also what makes skipping the voice pass safe. Prose that only lost a clause has not been re-voiced.

The trigger has to stay narrow. An unsupported claim the thesis rests on cannot be deleted; removing it leaves an article arguing nothing, and the honest outcome there is a factual gap for the author to close with more source material. The floors are the available proxy, and a re-score is the check — if the article comes back below a floor it was above, the cut was load-bearing and the round was owed after all.

**Also unresolved:** rubric floors are global rather than per content type, and the weighting profile in force is the default one. A technical explainer whose brief demands enumerations is graded on the same voice floor as a personal essay.

##### 13. Final validation

Passing the editorial score does not immediately make an article publishable.

The final stage performs deterministic or tightly constrained checks:

- No confidential names remain
- No prohibited terminology appears
- No unresolved placeholders remain
- Required facts are present
- Unsupported numbers have not been introduced
- The title matches the thesis
- Formatting matches the publishing platform
- Length remains within the approved range
- Markdown is valid
- Reserved material has not leaked from another article
- The exported version is the version that passed review

This stage does not creatively rewrite the article.

**Status: built, not yet exercised.** No run has reached it. Everything claimed about this stage is design, not observation.

##### 14. Human approval and export

The user sees:

- Final article
- Overall and dimension scores
- Remaining non-blocking concerns
- Revision count
- Version lineage
- Diff from the previous version
- Validation results
- Relevant execution history

The user may approve, manually edit, request another targeted revision, override the score, export, or abandon the article.

Initial export formats include Markdown, plain text, HTML, and clipboard-ready content.

**Status: built, not yet exercised.** The manual-edit path in particular has never run, and it is the path that would have finished the first article in four seconds.

---

#### The revision loop, stagnation, and stopping

The system prevents endless rewriting through a round ledger.

Defaults:

- Three substantive rewrite rounds
- Two style-only rounds
- One automatic architecture reopening
- Explicit user approval for additional rounds

Rounds are charged at the routing decision, not at the rewrite, and a run that exceeds its allowance parks in a stalled state with a reason rather than failing. The author then chooses: approve despite the score, add source material, narrow the thesis, reopen the brief, reopen the architecture, lower the threshold, authorise another round, or abandon the article.

The pipeline should also stop when:

- Scores improve by less than a small threshold across repeated rounds
- The same blocking issue survives multiple rewrites
- Scores oscillate between versions
- One dimension improves while another repeatedly declines
- The reviewer introduces unrelated new preferences
- The latest version is not materially better than its parent

**The first run exercised the cap and not the detector.** Three rounds were spent and the run parked correctly, but the fourth and fifth conditions above — one dimension improving while another declines, and a version no better than its parent — were both true from round two onward and the author found out by reading the score history manually.

The deeper finding is that **caps limit damage without preventing it.** The score declined monotonically across the rounds the cap allowed. A limit on how many times the loop may run is not a substitute for noticing that it is running the wrong way. Stagnation detection needs to be a routing input, not a report.

---

#### Personal voice system

Each user has a persistent voice profile based on operational writing rules rather than vague labels.

The profile may include:

- Preferred level of directness
- Formality
- Use of first person
- Sentence-length variation
- Technical depth
- Paragraph density
- Heading style
- Opening and conclusion preferences
- Punctuation rules
- Prohibited phrases or patterns

Voice rules have different strengths.

##### Hard rules

Rules that should rarely be violated, such as:

- Do not use the internal product name.
- Do not use em dashes.
- Do not invent quotations.
- Preserve technical qualifications.

##### Strong preferences

Rules that normally apply but allow justified exceptions.

##### Tendencies

Patterns common to the user's writing without forcing every article into the same structure.

The voice profile may be built from user-written articles, approved drafts, before-and-after edits, explicit rules, and examples the user likes or dislikes.

The system may detect recurring edit patterns, but it does not update the permanent voice profile automatically. It proposes a rule, shows the evidence, and waits for approval.

Style instructions may exist at global, project, and article levels:

```text
article override > project profile > global profile
```

The system should also watch for overfitting. Consistent voice is useful, but identical openings, section patterns, and conclusions across every article are not.

**Status: the profile and precedence are built; learning from edits is not.** No suggestion has ever been generated, because the proposal path depends on approved manual edits and no run has produced one.

**Unanticipated finding:** voice is the dimension that absorbs the cost of substantive rewriting. It was never the reason a round opened, and it finished the run one point above its failure floor. Style is not an independent axis when the article keeps being rewritten for other reasons.

---

#### Transparency and execution provenance

Transparency is a first-class product capability, not an extension of logging. This is the design decision that most clearly justified itself.

For every pipeline stage, the system preserves:

- Input artefact versions
- Exact request sent to the model
- Context included and excluded
- Prompt and rubric versions
- Model provider and model
- Generation settings
- Raw response
- Parsed response
- Validation failures
- Repair attempts
- Tool calls
- Scores
- Routing decisions
- Output artefacts
- User interventions
- Cost and execution time

This allows the user to determine whether a poor result came from the source model, context construction, prompt, model, repair process, scoring rubric, workflow rule, or manual decision.

**Every defect described in this document was found this way** — by querying stored executions, not by adding instrumentation or reproducing anything. The specific property that did the work was *linkage*: a score records the version it scored, a review records the version it read, and a plan records the review it consumed. Comparing three recorded identifiers turned "the reviewer seems confused" into a one-line fix.

**Caveats found in practice:**

Cost per invocation is recorded on every call and is zero on every call. Tool invocations are recorded and none has ever been written. A field that is present, typed, and permanently empty invites false conclusions more effectively than a missing one does.

Provenance answers questions you know to ask. It made every defect *provable in seconds once suspected*, and it surfaced none of them on its own. The scoring prompt returned one claim instead of five for three consecutive rounds; nothing flagged the pattern, and it was noticed by a person reading three score sheets side by side. The natural next capability is not more recording but a small number of standing questions asked of the record automatically.

##### Observable explanations

The system does not attempt to preserve a model's hidden reasoning.

Instead, decisions include concise, structured explanations based on observable evidence:

- Source references
- Article passages
- Rules triggered
- Alternatives considered
- Confidence
- Reasons for the selected route

##### Retries and repairs

Retries remain individually visible.

The system distinguishes between:

- Network retries
- Provider failures
- Rate limits
- Invalid structured output
- Content repair
- Model fallback
- Manual retry
- Retry after configuration changes

A prompt that succeeds only after several repairs is less reliable than one that succeeds on the first attempt.

##### Tool calls

Any external or internal tool that affects the result is recorded, including:

- Source parsers
- Retrieval
- Markdown validation
- Confidentiality checks
- Link validation
- Claim matching
- Diff generation
- Token counting

##### Inspect, replay, and fork

The product supports three distinct operations.

###### Inspect

View exactly what happened during the original execution.

###### Replay

Run the same stage again using the recorded configuration. Because hosted models may be nondeterministic, this creates a new linked execution rather than replacing the original.

###### Fork

Start from an earlier execution while changing one variable, such as the model, prompt, rubric, voice profile, or context-selection strategy.

Forking makes controlled pipeline improvement possible.

**Confirmed in practice, and more useful than expected.** A re-run that scores an existing version without moving the workflow is how a defective scoring prompt was proved: same article, same version, new prompt, one claim became five. The property that makes this work is that a re-run explicitly does not take a workflow edge. Re-executing a stage to *ask a question* and re-executing it to *advance the run* are different operations, and conflating them would have made every experiment a state change.

---

#### Product form and major design decisions

##### Local-first web application

The primary interface is a local-first web application.

A CLI is useful for imports, exports, testing, replay, and batch operations, but it is not well suited to the main workflow.

The product requires:

- Structured source editing
- Question queues
- Architecture manipulation
- Version comparison
- Review triage
- Score visualisation
- Execution inspection
- Branch comparison

These tasks are better handled through a visual interface.

##### Artefact-first rather than chat-first

The product is not organised around one long conversation.

The main interface exposes the actual artefacts:

- Sources
- Claims
- Questions
- Architectures
- Briefs
- Article versions
- Reviews
- Revision plans
- Scores
- Validation reports
- Execution traces

Chat may assist with instructions and clarification, but it is not the primary representation of project state.

##### The interface must not hold a second opinion of the workflow

A progress display is the easiest place for a frontend to grow its own model of the pipeline, and once it has one, the two disagree quietly.

So the workflow publishes what a person should be told — the phases, the one-line description of each state, and who the run is waiting on — and the frontend renders it without knowing state names, action names, or how to construct a command. This is enforced by a test rather than a convention.

**Learned during the run:** publishing a line per *state* is not sufficient, because the machine's states and the author's steps are different granularities. One state covered both "decide these findings" and "approve the resulting plan", and a person in the first half was given the instructions for the second. Where a state means more than one thing to a person, the line has to be chosen from the run's contents by the layer that can read them — never inferred by the screen.

**Learned the same way:** a state can wait for a person even when the transition table says otherwise. The edges out of the "revision required" state are actored *policy*, because policy chooses which stage a failure returns to, and a derived answer therefore reported that the pipeline was working. Nothing was working — no worker will ever pick that state up. The author watched a spinner that was not spinning. Derived answers are correct until the derivation has a blind spot, and the blind spot needs its own name in the code rather than a second list that will drift.

##### TypeScript frontend and Python backend

The frontend uses TypeScript because it is well suited to interactive web interfaces.

The backend uses Python because the project depends heavily on:

- LLM integrations
- Text processing
- Document parsing
- Evaluation tooling
- Retrieval
- Data analysis
- Prompt experimentation

This also gives the project meaningful backend domain logic rather than reducing it to a thin model API wrapper.

##### Explicit state machine instead of autonomous agents

The pipeline has known stages and valid transitions.

A state machine makes it easier to explain:

- Why a stage ran
- Why an article moved backwards
- Which retry limit applies
- Which human approval is required
- Which rule caused a failure
- Which actions are currently valid

An unrestricted agent architecture would make the workflow harder to test, debug, and trust.

**Strongly confirmed.** Every defect found was a defect in a *rule*, and rules can be read. The recurring failure mode was never "the model went off the rails" — it was "the model was asked a question nobody had specified", which is only diagnosable because the asking is explicit and recorded.

The related decision that paid off: gates are enforced by *absence* rather than by checks. A state with no automatic next step is one no worker can pick up, so a human pause cannot be bypassed by a caller who forgot to check. A rule expressed as a missing table entry cannot be forgotten at a call site.

##### Custom workflow logic before a heavy framework

The initial workflow is predictable enough to implement directly.

A specialised orchestration framework may become useful later if the product develops complex parallelism or distributed execution, but adding one too early would introduce infrastructure and abstractions before the editorial workflow has been validated.

**Held up.** The workflow needed roughly a dozen structural corrections during its first real run. Every one was a small change to a table, a prompt, or a guard. A framework would have supplied concepts to work around rather than any of the corrections.

##### Immutable and branching history

Every source model, brief, review, article version, and voice profile is stored as an immutable version.

History supports branches because prompt comparisons, model experiments, manual edits, and alternative rewrites may all start from the same parent version.

##### Provenance records instead of relying on logs

Application logs are useful for infrastructure problems, but they do not reliably describe editorial causality.

The provenance system is structured, queryable, linked to artefacts, and designed for inspection and comparison.

##### Prompts as versioned artefacts with declared inputs

Prompts live in a version-controlled store: numbered versions, a declared current version, a system prompt, notes recording why the version exists, and a list of the variables the template requires. Rendering is strict — an undefined variable is an error, not an empty string.

**This is the guard that caught the most.** When the revision planner was given the source model, the change failed at render until the required-variables list was updated. The declaration is a real check and not documentation.

**And it is the guard with the clearest gap.** Nothing checks the other direction: that every field a stage's schema allows the model to populate is described somewhere in the prompt. That gap is the single largest source of defects found on the first run, and it was found seven separate times before it was named.

---

#### Experimentation and improvement

The preserved execution history supports controlled comparisons.

Examples include:

- Prompt version A against prompt version B
- Model A against model B
- Full-source context against retrieval
- Different scoring rubrics
- Different voice profiles
- Direct rewriting against revision planning followed by rewriting

Useful measurements include:

- Human preference
- Factual errors
- Unsupported claims
- Number of revision rounds
- Final acceptance rate
- Cost
- Latency
- Structured-output failure rate
- Reviewer disagreement
- Amount of manual editing required

The difference between the pipeline's proposed final article and the user-approved version is particularly useful.

A high score followed by heavy manual editing may indicate that the scoring rubric is not measuring what the user actually values.

**Two measurements were unexpectedly diagnostic and should be promoted to first-class:**

*Findings returned versus findings that changed the article.* Ten to one on the first run. This ratio measures how much of the author's attention the system is spending, and it is the number the triage complaint reduces to.

*Score trajectory across rounds of a single article.* A monotonic decline across three rounds is a stronger signal than any single score, and nothing was watching for it.

---

#### Privacy and security

Because the system preserves detailed requests and responses, trace data may contain the same confidential information as the source material.

The product therefore:

- Stores project data locally by default
- Shows which provider receives which content
- Redacts secrets before persistence
- Never stores API keys or authentication headers in traces
- Respects confidentiality flags in prompts, outputs, and exports
- Supports full, redacted, metadata-only, and temporary trace-retention modes
- Allows project-level trace deletion
- Warns before exporting traces containing sensitive content

Transparency should not come at the cost of leaking the material the system was designed to protect.

The structural decision that makes this hold: redaction happens at a single chokepoint that owns persistence, rather than at each call site. A rule applied at N call sites is a rule that will be missed at the N+1th — a prediction the rest of this document independently confirms about prompts.

---

#### Where the product actually is

Built, tested, and exercised on a real run:

- Source ingestion, source-of-truth extraction, gap questions
- Architecture proposal and author override
- Brief generation and approval
- Drafting, substantive review, finding triage, revision planning, rewriting
- Voice alignment
- Scoring, routing, revision limits, stalling and escalation
- Immutable versions, execution provenance, replay and fork
- Web interface with journey display, review triage, score tables, diffs, lineage

Built and not yet exercised:

- Final validation
- Human approval and export
- Manual edits
- Voice learning from approved edits
- Evaluation datasets and experiment comparison

Known to need work, in rough order of how much the first run suffered for it:

1. A repair route for an article that fails only on removable claims, so a deletion does not cost a revision round.
2. A standing check that schema fields and prompt instructions agree, applied to every stage.
3. Stagnation detection as a routing input rather than a report.
4. Per-content-type rubric floors and weights.
5. An advisor that drafts the triage, with the author's confirmation still meaning something.
6. Reversible finding decisions, up to the point the plan consumes the review.
7. Validation of enumerated fields that are currently free strings.
8. An article-count constraint set before the architecture is proposed.

---

#### Key risks

##### Factual drift

Controlled through the source model, provenance, review, and final validation.

*Observed:* the controls work on the stages they were applied to. The risk that materialised was a stage nobody had classified as generative — the revision planner — asserting facts through its instructions. Classify by whether a stage's output reaches the reader, not by its name.

##### Reviewer inconsistency

Reduced through stable rubrics, score anchors, evidence-backed findings, version comparison, and optional repeated scoring.

*Observed:* the sharper risk is not the reviewer disagreeing with itself but the reviewer being asked about the wrong artefact. Scope every critique to the exact version under discussion, and prefer disagreements that are recorded as disagreements over silent ones.

##### Endless revision

Limited through rewrite caps, issue history, stagnation detection, and human escalation.

*Observed:* caps bound the cost and do not detect the direction. The article got worse for three consecutive rounds inside the cap. A cap answers "how long may this run" and never "is this working".

##### Style homogenisation

Reduced through article-level overrides and checks for repeated structures across recent articles.

*Untested:* only one article has been written.

##### Excessive questioning

Controlled by prioritising only blocking and high-value gaps.

*Held.* Gap questions were not a source of friction on the first run. Finding triage was.

##### Trace overload

Managed through summary views, expandable details, filters, and separate editorial and debugging modes.

##### Storage growth

Managed through compression, deduplication, retention policies, and content-addressed artefacts.

##### Confidential information in traces

Controlled through redaction before storage, retention choices, encryption, and sanitised exports.

##### False confidence in scores

Reduced by showing evidence, confidence, hard failures, score trends, and the final human decision separately.

*Observed, and inverted:* the more expensive failure was not the author trusting a good score. It was the *system* trusting a bad refusal — treating an unsupported six-word aphorism as equivalent to a fabricated mechanism, and spending a full revision round on it. Confidence needs calibrating in both directions.

##### A rule that lives only in a prompt

*New, and the most frequently realised risk on the first run.* Any constraint expressed only as prompt text holds until the model, the prompt version, or the schema changes, and its failure is silent because the output stays structurally valid. Wherever a rule can be a guard, it should be a guard; wherever it cannot, the prompt and the schema should be checked against each other automatically.

---

#### Final product position

GroundScribe is an inspectable editorial workflow for technical authors.

Its main responsibilities are:

- Preserve the truth of the source material
- Identify missing context
- Choose an appropriate article structure
- Keep each article within a clear boundary
- Produce and revise drafts
- Adapt to the author's voice
- Evaluate quality consistently
- Prevent endless revision
- Preserve every meaningful version
- Explain how each result was produced
- Support controlled comparison and improvement
- Keep the final publication decision with the user

The strongest form of the product remains a local-first, artefact-centred web application built around an explicit editorial state machine. Nothing in the first run argued against that shape; several things argued for it, because a pipeline whose rules are written down is a pipeline whose mistakes can be read.

What the first run added to the position is a correction of emphasis. The original claim was that the distinguishing feature is not generating an article but inspecting the process that produced it. That is still true, and it is incomplete. **Inspectability is what made the system improvable, but only because someone went looking.** The provenance record answered every question asked of it and volunteered nothing. The next version of this product's ambition is not more recording — it is for the system to notice, without being asked, that it has spent three rounds making an article worse.

Its distinguishing feature is not that it can generate an article. It is that the complete process that produced it can be inspected, understood, and improved — and the work now is to make the system a participant in that improvement rather than only its subject.
