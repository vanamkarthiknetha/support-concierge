// Mirrors the FastAPI response shapes. The console is a pure renderer of these:
// no triage logic lives in the frontend, ever. If the UI needs a field the trail
// doesn't expose, the fix goes in the backend — otherwise the API and the CLI
// drift apart and the audit trail stops being a single source of truth.

export type Route = "auto_resolve" | "draft_for_review" | "escalate";
export type Priority = "P0" | "P1" | "P2" | "P3";
export type ReviewAction = "approve" | "edit" | "reject";

export interface InjectionSpan {
  start: number;
  end: number;
  text: string;
  pattern: string;
}

export interface TicketRow {
  id: string;
  received_at: string;
  from_name: string | null;
  from_email: string | null;
  subject_raw: string | null;
  body_raw: string | null;
  subject_norm: string | null;
  body_norm: string | null;
  language: string | null;
  text_quality: number | null;
  repairs: string[];
  dedup_hash: string | null;
  related_tickets: string[];
  is_followup: boolean;
  injection_suspected: boolean;
  injection_spans: InjectionSpan[];
}

export interface AgentStep {
  id: string;
  seq: number;
  agent: string;
  model_id: string | null;
  prompt_hash: string | null;
  input: Record<string, unknown> | null;
  raw_output: string | null;
  parsed_output: Record<string, unknown> | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  attempt: number;
  error_type: string | null;
  error_detail: string | null;
}

export interface PolicyEvent {
  id: string;
  seq: number;
  rule_id: string;
  rule_version: string;
  triggered_by: string | null;
  route_before: Route | null;
  route_after: Route | null;
  detail: string | null;
}

export interface Decision {
  run_id: string;
  ticket_id: string;
  proposed_route: Route | null;
  policy_ceiling: Route | null;
  critic_verdict: Route | null;
  final_route: Route;
  escalation_queue: string | null;
  priority: Priority | null;
  c_margin: number | null;
  c_crossmodel: number | null;
  c_selfreport: number | null;
  penalties: Record<string, number>;
  composite: number | null;
  tau_auto: number;
  tau_draft: number;
  binding_constraint: string | null;
  rationale: string | null;
  contributors: Record<string, string>;
}

export interface Draft {
  run_id: string;
  ticket_id: string;
  template_id: string | null;
  subject: string | null;
  body: string | null;
  send: boolean;
  language: string | null;
  rationale: string | null;
  critique: {
    approved: boolean;
    issues: string[];
    promises_forbidden_action: boolean;
    asserts_unsupported_facts: boolean;
    language_mismatch: boolean;
    tone_appropriate: boolean;
    demote_to: Route | null;
    rationale: string;
  } | null;
}

export interface Review {
  id: string;
  reviewer: string;
  action: ReviewAction;
  draft_before: string | null;
  draft_after: string | null;
  reject_reason: string | null;
  acted_at: string;
}

export interface Run {
  id: string;
  ticket_id: string;
  graph_version: string;
  config_hash: string;
  started_at: string;
  ended_at: string | null;
  terminal_state: "running" | "completed" | "dead_lettered";
}

export interface Trace {
  ticket: TicketRow;
  run: Run | null;
  steps: AgentStep[];
  policy_events: PolicyEvent[];
  decision: Decision | null;
  draft: Draft | null;
  reviews: Review[];
}

export interface QueueItem {
  ticket_id: string;
  final_route: Route;
  composite: number | null;
  escalation_queue: string | null;
  priority: Priority | null;
  binding_constraint: string | null;
  run_id: string;
  subject_raw: string | null;
  from_name: string | null;
  from_email: string | null;
  received_at: string;
  injection_suspected: boolean;
  is_followup: boolean;
  language: string | null;
  draft_body: string | null;
  draft_subject: string | null;
  review_count: number;
}

export interface QueueResponse {
  route: Route;
  count: number;
  items: QueueItem[];
}

export interface Stats {
  counts: {
    tickets: number;
    runs: number;
    steps: number;
    policy_events: number;
    dead_letters: number;
    auto_resolved: number;
    reviews: number;
  };
  automation_rate: number;
  route_distribution: { final_route: Route; n: number; avg_conf: number | null }[];
  policy_rule_fires: { rule_id: string; n: number }[];
  override_rate: {
    final_route: Route;
    reviewed: number;
    rejected: number;
    edited: number;
    reject_rate: number | null;
  }[];
  calibration: { conf_decile: number; n: number; reviewed: number; rejected: number }[];
  agent_failures: { agent: string; error_type: string; n: number }[];
  config: {
    models: { cheap: string; smart: string };
    tau_auto: number;
    tau_draft: number;
    config_hash: string;
  };
}
