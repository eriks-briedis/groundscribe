/**
 * Sample payloads, typed against the generated client (phase 11).
 *
 * Typed on purpose: these are the shapes the screens are tested against, so if
 * the contract changes under them the fixtures stop compiling. That is the
 * cheapest form of the drift test the plan asks for — the compiler notices
 * before any assertion runs.
 *
 * The values are the golden article the backend's own suites use, so a person
 * reading a failure here recognises the data from the other side.
 */
import type {
  ArchitectureBoard,
  ArticleWorkspace,
  Dashboard,
  ExecutionComparison,
  LineageGraph,
  QuestionQueue,
  ReviewHistory,
  SourceWorkspace,
  StageInspection,
  TraceView,
} from '@/api/client';

export const PROJECT_ID = 'p1';
export const ARTICLE_ID = 'a1';
export const EXECUTION_ID = 'e1';

export const dashboard: Dashboard = {
  project: {
    id: PROJECT_ID,
    title: 'Read-through caching',
    description: 'How the render pipeline got faster.',
    author_id: 'ada',
  },
  run_id: 'r1',
  state: 'human_approval_required',
  available_actions: ['approve_final', 'cancel', 'fork_execution', 'reject_final'],
  constraints: {
    audience: 'senior backend engineers',
    platform: 'personal blog',
    depth: 'practitioner',
    target_length_words: 400,
    first_person_allowed: true,
    allowed_providers: ['ollama'],
    confidential_names: ['Project Halide'],
    trace_retention_consent: true,
  },
  source: {
    documents: 1,
    confidential_documents: 0,
    segments: 12,
    claims: 7,
    unresolved_questions: 1,
    answered_questions: 2,
  },
  articles: [
    {
      id: ARTICLE_ID,
      title: 'Read-through caching for the render pipeline',
      status: 'draft',
      versions: 3,
      rewrite_rounds: 1,
      open_findings: 0,
      validated: true,
      latest_score: {
        execution_id: EXECUTION_ID,
        overall: 89.5,
        passed: true,
        rubric_version: '1.0',
        evaluator_version: '1.0',
        dimensions: { factual_fidelity: 92, scope_discipline: 88 },
        failures: [],
        created_at: '2026-07-25T12:04:00Z',
      },
    },
  ],
  questions: [
    {
      id: 'g1',
      question: 'What was the cold-cache p99?',
      why_it_matters: 'The headline number is meaningless without it.',
      description: 'The source gives warm-cache latency only.',
      priority: 'blocking',
      group: 'latency',
      ordinal: 0,
      surfaced: true,
      resolved: false,
      answer: null,
    },
  ],
  active_jobs: [
    { id: 'job-1', job_type: 'score_article', status: 'running', attempts: 1, created_at: '2026-07-25T12:03:00Z' },
  ],
  recent_failures: [
    {
      execution_id: 'e0',
      stage: 'extract_source_truth',
      error_type: 'ProviderError',
      error_message: 'the provider timed out after 30s',
      occurred_at: '2026-07-25T11:00:00Z',
    },
  ],
  usage: { model_calls: 9, input_tokens: 10800, output_tokens: 7200, cost_usd: 0.108 },
};

export const sourceWorkspace: SourceWorkspace = {
  documents: [
    {
      id: 'd1',
      title: 'Read-through caching for the render pipeline',
      source_format: 'markdown',
      media_type: 'text/markdown',
      uri: null,
      confidential: true,
      content_hash: 'sha256:abc',
      created_by_execution_id: 'e0',
      segments: [
        { id: 's1', ordinal: 0, kind: 'paragraph', text: 'We cache rendered fragments.', char_start: 0, char_end: 28 },
        { id: 's2', ordinal: 1, kind: 'paragraph', text: 'p99 fell to 120ms on warm cache.', char_start: 29, char_end: 61 },
      ],
    },
  ],
  claims: [
    {
      id: 'c1',
      text: 'p99 latency fell to 120ms on warm cache.',
      classification: 'measured',
      segment_ids: ['s2'],
    },
  ],
  unknowns: [
    {
      id: 'g1',
      question: 'What was the cold-cache p99?',
      why_it_matters: 'The headline number is meaningless without it.',
      description: '',
      priority: 'blocking',
      group: 'latency',
      ordinal: 0,
      surfaced: true,
      resolved: false,
      answer: null,
    },
  ],
  source_model: { summary: 'A read-through cache in front of the fragment renderer.' },
  provider_visibility: dashboard.constraints,
  provenance: {
    source_model_execution_id: 'e2',
    source_model_snapshot_id: 'snap-2',
    extracted_at: '2026-07-25T11:30:00Z',
  },
};

export const questionQueue: QuestionQueue = {
  questions: [
    {
      id: 'g1',
      question: 'What was the cold-cache p99?',
      why_it_matters: 'The headline number is meaningless without it.',
      description: '',
      priority: 'blocking',
      group: 'latency',
      ordinal: 0,
      surfaced: true,
      resolved: false,
      answer: null,
    },
    {
      id: 'g2',
      question: 'Which parser was replaced?',
      why_it_matters: 'It bounds the claim to one code path.',
      description: '',
      priority: 'optional',
      group: 'scope',
      ordinal: 1,
      surfaced: true,
      resolved: true,
      answer: {
        text: 'The colour parser only.',
        question: 'Which parser was replaced?',
        why_it_matters: 'It bounds the claim to one code path.',
        response_type: 'answered',
        answered_by: 'ada',
        diff_snapshot_id: 'snap-3',
      },
    },
  ],
};

export const architecture: ArchitectureBoard = {
  current_version_id: 'arch-2',
  versions: [
    {
      id: 'arch-1',
      summary: 'One article about the cache.',
      locked: false,
      locked_by: null,
      parent_id: null,
      created_by_execution_id: 'e3',
      concepts: [
        { id: ARTICLE_ID, title: 'Read-through caching', angle: 'practitioner', thesis: 'Caching bought the latency.', ordinal: 0 },
      ],
    },
    {
      id: 'arch-2',
      summary: 'One article about the cache, one about invalidation.',
      locked: true,
      locked_by: 'ada',
      parent_id: 'arch-1',
      created_by_execution_id: 'e4',
      concepts: [
        { id: ARTICLE_ID, title: 'Read-through caching', angle: 'practitioner', thesis: 'Caching bought the latency.', ordinal: 0 },
        { id: 'a2', title: 'Invalidation', angle: 'practitioner', thesis: 'Invalidation is the hard half.', ordinal: 1 },
      ],
    },
  ],
  proposal: { decision: { selected: ARTICLE_ID, rationale: 'The measured claim is the strongest.' } },
};

export const articleWorkspace: ArticleWorkspace = {
  article: { id: ARTICLE_ID, project_id: PROJECT_ID, title: 'Read-through caching', status: 'draft' },
  run_id: 'r1',
  state: 'human_approval_required',
  available_actions: ['approve_final', 'cancel', 'fork_execution', 'reject_final'],
  brief: { thesis: 'Caching bought the latency, invalidation cost it back.', target_length_words: 400 },
  current_version: {
    id: 'v3',
    ordinal: 2,
    title: 'Read-through caching for the render pipeline',
    thesis: 'Caching bought the latency.',
    body: 'We cached fragments.\nThat number is why anyone would read this.',
    snapshot_id: 'snap-9',
    parent_id: 'v2',
    created_by_execution_id: 'e9',
  },
  previous_version: {
    id: 'v2',
    ordinal: 1,
    title: 'Read-through caching for the render pipeline',
    thesis: 'Caching bought the latency.',
    body: 'We cached fragments.\nThat number is the reason anyone would read this.',
    snapshot_id: 'snap-8',
    parent_id: 'v1',
    created_by_execution_id: 'e8',
  },
  diff: {
    added: 1,
    removed: 1,
    lines: [
      { kind: 'equal', text: 'We cached fragments.' },
      { kind: 'removed', text: 'That number is the reason anyone would read this.' },
      { kind: 'added', text: 'That number is why anyone would read this.' },
    ],
  },
  findings: [
    {
      id: 'i1',
      ref: 'i1',
      severity: 'blocking',
      category: 'factual',
      location: 'opening',
      passage: 'p99 fell to 120ms',
      description: 'The latency figure is stated without the cache condition.',
      evidence: 'The source marks it warm-cache only.',
      source_ref: 'c1',
      brief_ref: '',
      recommended_correction: 'Name the warm-cache condition.',
      suggested_route: 'factual_error',
      blocks_publication: true,
      reviewer_confidence: 0.9,
      fingerprint: 'fp-1',
      status: 'accepted',
      decided_by: 'ada',
      decision_reason: '',
      lifecycle: 'new',
    },
  ],
  revision_plan: { summary: 'Qualify the latency figure; trim the determinism aside.' },
  voice: {
    sources: ['ada@2 (global)'],
    active: [
      {
        instruction_id: 'no-em-dash',
        category: 'punctuation',
        strength: 'hard_rule',
        instruction: 'Never use an em dash.',
        source: 'ada@2 (global)',
        overrides: '',
      },
    ],
    suppressed: [],
  },
  scores: [
    {
      execution_id: 'e7',
      overall: 82,
      passed: false,
      rubric_version: '1.0',
      evaluator_version: '1.0',
      dimensions: { factual_fidelity: 78, scope_discipline: 80 },
      failures: [{ detail: 'factual_fidelity below its minimum' }],
      created_at: '2026-07-25T12:00:00Z',
    },
    {
      execution_id: EXECUTION_ID,
      overall: 89.5,
      passed: true,
      rubric_version: '1.0',
      evaluator_version: '1.0',
      dimensions: { factual_fidelity: 92, scope_discipline: 88 },
      failures: [],
      created_at: '2026-07-25T12:04:00Z',
    },
  ],
  validation: {
    passed: true,
    validator_version: '1.0',
    checks_run: ['confidential_names', 'length_in_range', 'content_hash'],
    findings: [],
    corrections: [],
  },
  producing_execution: {
    id: 'e9',
    stage: 'align_voice',
    impl_version: '1.0',
    ordinal: 9,
    status: 'succeeded',
    started_at: '2026-07-25T12:02:00Z',
    completed_at: '2026-07-25T12:02:30Z',
    error_type: null,
    error_message: null,
  },
  lineage: {
    nodes: [
      { id: 'v1', kind: 'article_version', label: 'v0', ordinal: 0, execution_id: 'e6' },
      { id: 'v2', kind: 'article_version', label: 'v1', ordinal: 1, execution_id: 'e8' },
      { id: 'v3', kind: 'article_version', label: 'v2', ordinal: 2, execution_id: 'e9' },
    ],
    edges: [
      { from: 'v1', to: 'v2', kind: 'supersedes' },
      { from: 'v2', to: 'v3', kind: 'supersedes' },
    ],
  },
  approval: {
    rewrite_rounds: 1,
    remaining_concerns: ['The latency figure is stated without the cache condition.'],
    interventions: [
      {
        id: 'int-1',
        intervention_type: 'approval',
        user_id: 'ada',
        occurred_at: '2026-07-25T11:50:00Z',
        payload: { action: 'approve_brief' },
      },
    ],
    model_versions: [
      {
        stage: 'generate_initial_draft',
        provider: 'ollama',
        model: 'llama3.1:70b-instruct',
        template_id: 'generate_initial_draft',
        template_version: '1.0',
      },
    ],
    usage: { model_calls: 9, input_tokens: 10800, output_tokens: 7200, cost_usd: 0.108 },
  },
};

export const reviewHistory: ReviewHistory = {
  rounds: [
    {
      review_id: 'rev-1',
      round: 0,
      verdict: 'revise',
      version_id: 'v1',
      version_ordinal: 0,
      execution_id: 'e5',
      issues: [articleWorkspace.findings?.[0] ?? []].flat(),
    },
    {
      review_id: 'rev-2',
      round: 0,
      verdict: 'polish',
      version_id: 'v2',
      version_ordinal: 1,
      execution_id: 'e8',
      issues: [
        {
          id: 'i2',
          ref: 'i1',
          severity: 'minor',
          category: 'clarity',
          location: 'section 2',
          passage: 'the determinism aside',
          description: 'The aside runs long.',
          evidence: '',
          source_ref: '',
          brief_ref: '',
          recommended_correction: 'Trim it.',
          suggested_route: 'polish',
          blocks_publication: false,
          reviewer_confidence: 0.6,
          fingerprint: 'fp-2',
          status: 'proposed',
          decided_by: '',
          decision_reason: '',
          lifecycle: 'repeated',
        },
      ],
    },
  ],
  scores: articleWorkspace.scores ?? [],
  warnings: ['two rounds have not moved the score'],
};

export const lineage: LineageGraph = articleWorkspace.lineage ?? { nodes: [], edges: [] };

export const trace: TraceView = {
  filters_applied: [],
  executions: [
    {
      id: 'e2',
      stage: 'extract_source_truth',
      impl_version: '1.1',
      ordinal: 2,
      status: 'succeeded',
      started_at: '2026-07-25T11:29:00Z',
      completed_at: '2026-07-25T11:30:00Z',
      error_type: null,
      error_message: null,
      events: 4,
      invocations: 1,
      usage: { model_calls: 1, input_tokens: 1200, output_tokens: 800, cost_usd: 0.012 },
      matched_filters: [],
    },
    {
      id: 'e0',
      stage: 'extract_source_truth',
      impl_version: '1.1',
      ordinal: 0,
      status: 'failed',
      started_at: '2026-07-25T11:00:00Z',
      completed_at: '2026-07-25T11:00:30Z',
      error_type: 'ProviderError',
      error_message: 'the provider timed out after 30s',
      events: 3,
      invocations: 2,
      usage: { model_calls: 2, input_tokens: 2400, output_tokens: 0, cost_usd: 0.06 },
      matched_filters: ['failed', 'high_cost'],
    },
  ],
};

export const inspection: StageInspection = {
  summary: {
    id: EXECUTION_ID,
    stage: 'extract_source_truth',
    impl_version: '1.1',
    ordinal: 2,
    status: 'succeeded',
    started_at: '2026-07-25T11:29:00Z',
    completed_at: '2026-07-25T11:30:00Z',
    error_type: null,
    error_message: null,
  },
  inputs: [
    {
      snapshot_id: 'snap-1',
      artifact_type: 'source_document',
      role: 'source',
      direction: 'input',
      ordinal: 0,
      content_hash: 'sha256:aaa',
      size: 2048,
      content: { title: 'Read-through caching for the render pipeline' },
    },
  ],
  outputs: [
    {
      snapshot_id: 'snap-2',
      artifact_type: 'source_model',
      role: 'source_model',
      direction: 'output',
      ordinal: 0,
      content_hash: 'sha256:bbb',
      size: 4096,
      content: { summary: 'A read-through cache in front of the fragment renderer.' },
    },
  ],
  context_selections: [
    {
      id: 'ctx-1',
      strategy: 'whole_document',
      strategy_version: '1.0',
      token_budget: 8000,
      items: [
        { ordinal: 0, reference: 'segment:s1', disposition: 'selected', reason: '', score: 0.9 },
        { ordinal: 1, reference: 'segment:s9', disposition: 'excluded', reason: 'over budget', score: 0.1 },
      ],
    },
  ],
  invocations: [
    {
      id: 'inv-1',
      parent_invocation_id: null,
      attempt_ordinal: 1,
      retry_type: null,
      outcome: 'accepted',
      provider: 'ollama',
      model: 'llama3.1:70b-instruct',
      template_id: 'extract_source_truth',
      template_version: '1.2',
      input_tokens: 1200,
      output_tokens: 800,
      cost_usd: 0.012,
      started_at: '2026-07-25T11:29:10Z',
      completed_at: '2026-07-25T11:29:50Z',
      error_message: null,
      effective_request: { template_id: 'extract_source_truth', messages: [{ role: 'user', content: 'Extract…' }] },
      raw_response: '{"summary": "A read-through cache…"}',
      parsed_response: { summary: 'A read-through cache…' },
      validated_response: { summary: 'A read-through cache…' },
    },
  ],
  tool_calls: [],
  decisions: [
    {
      id: 'dec-1',
      decision_type: 'gap_prioritisation',
      decided_by: 'generate_gap_questions',
      decided_by_type: 'policy',
      policy_version: '1.0',
      inputs: { gaps: 2 },
      outcome: 'surfaced 1 of 2',
      rationale: 'only the blocking gap stops the run',
      decided_at: '2026-07-25T11:30:00Z',
    },
  ],
  evaluations: [],
  interventions: [],
  events: [
    {
      id: 'ev-1',
      event_type: 'stage.started',
      timestamp: '2026-07-25T11:29:00Z',
      actor_type: 'system',
      actor_id: 'pipeline',
      payload: {},
      correlation_id: 'c1',
      causation_id: null,
      sequence: 0,
    },
    {
      id: 'ev-2',
      event_type: 'stage.completed',
      timestamp: '2026-07-25T11:30:00Z',
      actor_type: 'system',
      actor_id: 'pipeline',
      payload: { artefacts: 1 },
      correlation_id: 'c1',
      causation_id: 'ev-1',
      sequence: 3,
    },
  ],
  usage: { model_calls: 1, input_tokens: 1200, output_tokens: 800, cost_usd: 0.012 },
  duration_ms: 60000,
  error: null,
};

export const comparison: ExecutionComparison = {
  left: {
    id: 'e0',
    pipeline_run_id: 'r1',
    parent_execution_id: null,
    stage: 'extract_source_truth',
    impl_version: '1.1',
    ordinal: 0,
    status: 'failed',
    correlation_id: 'c1',
    started_at: '2026-07-25T11:00:00Z',
    completed_at: '2026-07-25T11:00:30Z',
    error_type: 'ProviderError',
    error_message: 'the provider timed out after 30s',
  },
  right: {
    id: 'e2',
    pipeline_run_id: 'r1',
    parent_execution_id: null,
    stage: 'extract_source_truth',
    impl_version: '1.1',
    ordinal: 2,
    status: 'succeeded',
    correlation_id: 'c1',
    started_at: '2026-07-25T11:29:00Z',
    completed_at: '2026-07-25T11:30:00Z',
    error_type: null,
    error_message: null,
  },
  differences: [
    { field: 'stage', left: 'extract_source_truth', right: 'extract_source_truth', same: true },
    { field: 'status', left: 'failed', right: 'succeeded', same: false },
    { field: 'model', left: 'llama3.1:70b-instruct', right: 'llama3.1:70b-instruct', same: true },
    { field: 'cost_usd', left: '0.06', right: '0.012', same: false },
    { field: 'latency_ms', left: '30000', right: '60000', same: false },
  ],
  output_edit_distance: 14,
};
