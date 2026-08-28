-- Audit schema. The bar from the brief:
--   "We should be able to look at any ticket after the fact and reconstruct
--    exactly what happened and why."
--
-- That is a QUERY, not a log file -- which is why this is a normalized schema
-- rather than one JSON blob per ticket. See ADR-009.

CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
    CREATE TYPE route AS ENUM ('auto_resolve', 'draft_for_review', 'escalate');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE review_action AS ENUM ('approve', 'edit', 'reject');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE run_state AS ENUM ('running', 'completed', 'dead_lettered');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- The ticket as received, plus what normalization made of it. Both are kept:
-- you cannot audit a decision if you only stored the cleaned-up input.
CREATE TABLE IF NOT EXISTS tickets (
  id                  TEXT PRIMARY KEY,
  received_at         TIMESTAMPTZ NOT NULL,
  from_name           TEXT,
  from_email          CITEXT,
  subject_raw         TEXT,
  body_raw            TEXT,
  subject_norm        TEXT,
  body_norm           TEXT,
  language            TEXT,
  text_quality        REAL,
  repairs             JSONB DEFAULT '[]'::jsonb,
  dedup_hash          TEXT,
  related_tickets     JSONB DEFAULT '[]'::jsonb,
  is_followup         BOOLEAN NOT NULL DEFAULT FALSE,
  injection_suspected BOOLEAN NOT NULL DEFAULT FALSE,
  injection_spans     JSONB DEFAULT '[]'::jsonb,   -- quarantined; never re-prompted
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tickets_dedup_idx  ON tickets (dedup_hash);
CREATE INDEX IF NOT EXISTS tickets_sender_idx ON tickets (from_email, received_at DESC);


-- One row per pipeline execution. Re-runs INSERT; they never overwrite.
CREATE TABLE IF NOT EXISTS runs (
  id             UUID PRIMARY KEY,
  ticket_id      TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  graph_version  TEXT NOT NULL,
  config_hash    TEXT NOT NULL,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at       TIMESTAMPTZ,
  terminal_state run_state NOT NULL DEFAULT 'running',
  total_tokens   INTEGER,
  total_cost_usd NUMERIC(12, 8)
);
CREATE INDEX IF NOT EXISTS runs_ticket_idx ON runs (ticket_id, started_at DESC);


-- One row per agent invocation, INCLUDING failed attempts. A ticket that escalated
-- because the classifier timed out twice is indistinguishable from a genuine
-- escalation unless those failures are rows.
CREATE TABLE IF NOT EXISTS agent_steps (
  id            UUID PRIMARY KEY,
  run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq           SMALLINT NOT NULL,
  agent         TEXT NOT NULL,
  model_id      TEXT,          -- pinned exact id
  prompt_hash   TEXT,          -- ties this output to an exact prompt version
  input         JSONB,
  raw_output    TEXT,
  parsed_output JSONB,
  latency_ms    INTEGER,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  attempt       SMALLINT NOT NULL DEFAULT 1,
  error_type    TEXT,
  error_detail  TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, seq, attempt)
);
CREATE INDEX IF NOT EXISTS steps_run_idx   ON agent_steps (run_id, seq);
CREATE INDEX IF NOT EXISTS steps_error_idx ON agent_steps (agent, error_type)
  WHERE error_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS steps_out_gin   ON agent_steps USING GIN (parsed_output);


-- The routing decision and every number behind it.
CREATE TABLE IF NOT EXISTS decisions (
  run_id             UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  ticket_id          TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  proposed_route     route,
  policy_ceiling     route,
  critic_verdict     route,
  final_route        route NOT NULL,
  escalation_queue   TEXT,
  priority           TEXT,
  c_margin           REAL,
  c_crossmodel       REAL,
  c_selfreport       REAL,
  penalties          JSONB DEFAULT '{}'::jsonb,
  composite          REAL,
  tau_auto           REAL NOT NULL,   -- thresholds IN FORCE AT THE TIME
  tau_draft          REAL NOT NULL,
  binding_constraint TEXT,            -- the one-line "why not auto-resolved?"
  rationale          TEXT,
  contributors       JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS decisions_route_idx ON decisions (final_route, composite);


-- EVERY rule that fired, not just the first match. TCK-1010 fires both
-- legal_request and account_deletion; if one is later found miscalibrated,
-- the trail still shows the ticket was independently covered.
CREATE TABLE IF NOT EXISTS policy_events (
  id           UUID PRIMARY KEY,
  run_id       UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ticket_id    TEXT NOT NULL,
  seq          SMALLINT NOT NULL DEFAULT 0,
  rule_id      TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  triggered_by TEXT,
  route_before route,
  route_after  route,
  detail       TEXT
);
CREATE INDEX IF NOT EXISTS policy_rule_idx ON policy_events (rule_id);
CREATE INDEX IF NOT EXISTS policy_run_idx  ON policy_events (run_id);


-- Generated drafts. Kept separately from `decisions` because a draft can be
-- edited by a human and we need the before/after pair.
CREATE TABLE IF NOT EXISTS drafts (
  run_id      UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  ticket_id   TEXT NOT NULL,
  template_id TEXT,
  subject     TEXT,
  body        TEXT,
  send        BOOLEAN NOT NULL DEFAULT TRUE,
  language    TEXT,
  rationale   TEXT,
  critique    JSONB
);


-- What the human actually did. `action = 'reject'` is the best ground-truth
-- quality signal the system gets, and it is free.
CREATE TABLE IF NOT EXISTS reviews (
  id            UUID PRIMARY KEY,
  ticket_id     TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  reviewer      TEXT NOT NULL,
  action        review_action NOT NULL,
  draft_before  TEXT,
  draft_after   TEXT,
  reject_reason TEXT,
  acted_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reviews_action_idx ON reviews (action, acted_at DESC);
CREATE INDEX IF NOT EXISTS reviews_ticket_idx ON reviews (ticket_id);


-- Nothing is ever silently dropped. Every ticket reaches a terminal state.
CREATE TABLE IF NOT EXISTS dead_letters (
  id             UUID PRIMARY KEY,
  ticket_id      TEXT,
  run_id         UUID,
  stage          TEXT,
  exception_type TEXT,
  traceback      TEXT,
  state_snapshot JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
