### MAGNETO — Opportunity Research & Product Validation Hub

A self-hosted research system for turning messy market evidence into product opportunities that are specific enough to evaluate, rank, and validate. MAGNETO does not stop at “interesting signals.” It normalizes workflow pain, clusters repeated problems, filters for buyer-backed and
  automatable gaps, generates constrained product ideas, then promotes the strongest ones into a Product Hub where positioning, evidence, risks, interviews, prospects, and validation work can be managed.

The project originally had a broad signal pipeline: extract workflow pain from many evidence sources, normalize it, cluster it, and generate ideas. That worked for discovering raw patterns, but it created too much noise. A “signal” could be a one-off complaint, a vague workflow label, or a
  useful fragment without enough buyer context. The later opportunity pipeline was introduced to separate “evidence exists” from “this is a product-shaped opportunity.”

What it does

- 1. Turns raw evidence into structured workflow pain: actor, workflow, pain point, workaround, urgency, frequency, tools, buyer context, and automation potential.
  2. Normalizes workflows through a controlled taxonomy so recurring problems cluster together instead of fragmenting into near-duplicate labels.
  3. Enriches signals into opportunity context: task family, buyer role, industry bucket, trigger event, artifact/object type, step gaps, and likely intervention points.
  4. Clusters enriched signals into opportunities, not just workflow mentions.
  5. Scores opportunities by commercial and technical quality: pain, frequency, buyer budget, feasibility, distribution, moat, data access, and evidence strength.
  6. Decomposes workflows into steps so the system can reason about where software should assist, replace, or avoid automating.
  7. Generates product ideas only after enough opportunity context exists, then critiques and ranks them before they reach the product layer.
  8. Supports vertical-focused research through industry profiles, such as real-estate-property, to avoid mixing unrelated markets.
  9. Promotes qualified ideas into Products with structured briefs, issues, resources, interviews, linked evidence, prospects, exports, and product-context chat.
  10. Provides a MAGNETO web UI for reviewing opportunities, merging duplicates, inspecting evidence, creating products, managing validation work, and monitoring pipeline runs.

Why The Opportunity Pipeline Replaced The Signal Pipeline

The signal pipeline was useful but too literal. It answered: “What workflow pains were mentioned?” That was not enough. The product question is harder: “Which repeated workflow gaps are specific, urgent, buyer-owned, technically feasible, and commercially worth building around?”

The opportunity pipeline was added because the signal layer had three failure modes:

- 1. Fragmentation: similar pains were split across labels like rent collection, payment follow-up, invoice chasing, and accounts receivable management.
  2. Generic labels: LLM extraction produced broad labels such as process management or administrative support, which were too vague to generate useful products from.
  3. Premature ideation: ideas were being generated from clusters before the system understood workflow steps, buyer authority, data access, or whether the pain was repeated enough.

The opportunity pipeline fixes this by inserting stricter curation between evidence and product ideation. It enriches, filters, clusters, merges, decomposes, scores, and critiques before promoting anything into the Product Hub.

Key Technical Decisions

Controlled taxonomy over free-form clustering

- - Free-form LLM labels caused duplicate and generic clusters.
  - MAGNETO uses a curated workflow taxonomy, canonical label maps, blocked generic labels, and proposal queues.
  - New labels are staged for review instead of immediately entering the opportunity graph.
  - This keeps the system from building product ideas on top of weak abstractions.

Opportunity clusters over signal clusters

- - Signal clusters group by workflow mention.
  - Opportunity clusters group by product-relevant context: buyer, task, artifact, industry, trigger, workflow step, and pain shape.
  - This was necessary because the same workflow label can represent different opportunities in different verticals.

Industry profiles over global runs

- - Full-corpus enrichment wastes model calls and blends unrelated markets.
  - Industry profiles define reusable filters for a vertical, using category, industry, buyer role, and title/context rules.
  - The real-estate-property profile exists because property-management signals appeared outside obvious property categories, so a simple category filter was too narrow.

Step decomposition before ideation

- - The system decomposes a workflow before generating products.
  - This makes ideation more grounded: the product can target a specific broken step instead of vaguely “automating the workflow.”
  - It also exposes whether the hard part is data access, accuracy, human judgment, integrations, or distribution.

Critic and scoring passes before Product Hub

- - The Product Hub is for validation candidates, not every generated idea.
  - Ideas are scored and criticized first so weak ideas can be killed before they become operational clutter.
  - This keeps the product layer focused on things worth researching further.

Local LLM gateway over direct provider coupling

- - Pipeline scripts need reliable, repeated LLM calls with retries, timeout handling, model routing, and normalized responses.
  - MAGNETO routes calls through lib/llm.js, with support for a local codex-harness gateway.
  - The gateway owns provider/auth quirks, while the app only depends on a simple local request/response contract.
  - This made high-volume pipeline work easier to test and less tied to one provider implementation.

Product Hub as a separate layer

- - Ideas alone were not enough to support real product work.
  - Product candidates need positioning, pricing, promises, non-promises, MVP scope, risks, interviews, resources, prospects, and exported briefs.
  - The Product Hub turns a ranked idea into a validation workspace.

Product Hub

Products sit above the opportunity pipeline:

product ← idea ← opportunity ← signals

A Product contains:

- - core promise and non-promise
  - market focus and positioning
  - structured product brief sections
  - linked opportunity and evidence
  - MVP scope and feature decisions
  - pricing and GTM assumptions
  - technical implementation notes
  - risks, assumptions, blockers, and questions
  - resources and competitor notes
  - customer discovery interviews
  - prospects and outreach status
  - product-context chat
  - Markdown/template export

This design keeps discovery and validation connected. A product is not a blank note; it carries the evidence, opportunity context, and critique that produced it.

Technical Challenges And Resolutions

Noisy LLM output

- - Challenge: LLMs produced malformed JSON, vague labels, duplicate concepts, and overconfident ideas.
  - Resolution: strict prompts, JSON parsing repair, taxonomy constraints, proposal staging, generic-label blocking, dry-runs, debug modes, and critic passes.

Taxonomy drift

- - Challenge: workflow labels multiplied as new evidence entered the system.
  - Resolution: canonical workflow maps, taxonomy cleanup docs, proposal review endpoints, candidate sightings, and merge tooling.

Cluster quality

- - Challenge: early clusters grouped related words, not necessarily real opportunities.
  - Resolution: opportunity-specific clustering added buyer/task/artifact/context facets and merge workflows.

Cost and latency

- - Challenge: full runs over large evidence sets required many LLM calls.
  - Resolution: model tiers, batching, concurrency controls, prompt grouping for cache hits, dry-run modes, script limits, and industry-profile filtering.

Overlarge server surface

- - Challenge: the server became hard to extend as routes, pipeline controls, UI data, and product features accumulated.
  - Resolution: the server was refactored into app.js, route modules, run manager, signal runner, scheduler, capabilities, utilities, and consistent JSON error handling.

Premature product creation

- - Challenge: generated ideas could look plausible while lacking buyer proof or buildability.
  - Resolution: MAGNETO added scoring passes for buyer budget, distribution, moat, feasibility, and final rank before Product Hub promotion.

Why This Shape Matters

MAGNETO is designed around a stricter research funnel:

evidence
  → structured signals
  → enriched opportunity context
  → curated opportunity clusters
  → workflow decomposition
  → scored product ideas
  → Product Hub validation

The important decision is that product ideas are not the primary artifact. They are the output of a pipeline that first tries to prove the opportunity deserves attention. That makes the system more useful as a founder/research tool: it reduces idea noise, preserves evidence, and keeps
  validation work tied to the market signals that justified it.