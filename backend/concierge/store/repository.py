"""Persistence for the audit trail.

Write side records everything; read side answers the questions the brief's
monitoring section demands. Both matter -- the store is READ during triage too
(follow-up detection, TCK-1014), not only written to.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from concierge.config import get_settings
from concierge.models import AgentStep, Draft, Ticket, TriageState

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _uuid() -> str:
    return str(uuid.uuid4())


class Repository:
    """Thin psycopg3 wrapper. No ORM: the queries here are the deliverable, and
    burying them behind a session object would hide the thing being demonstrated."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or get_settings().psycopg_url

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    # --- schema ------------------------------------------------------------------

    def init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.execute(sql)
            conn.commit()

    def reset(self) -> None:
        with self.connect() as conn:
            conn.execute(
                "TRUNCATE reviews, drafts, policy_events, decisions, agent_steps, "
                "runs, dead_letters, tickets RESTART IDENTITY CASCADE"
            )
            conn.commit()

    # --- read side used DURING triage --------------------------------------------

    def find_related(self, from_email: str | None, dedup_hash: str) -> list[str]:
        """Prior tickets from the same sender with the same normalized content.

        Statelessness here is a correctness bug, not a missed optimisation: sending
        the same canned bug acknowledgement twice (TCK-1002 then TCK-1014) is the
        clearest possible signal that nobody is reading.
        """
        if not from_email:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM tickets
                WHERE from_email = %s AND (dedup_hash = %s OR subject_norm IS NOT NULL)
                ORDER BY received_at DESC LIMIT 10
                """,
                (from_email, dedup_hash),
            ).fetchall()
        return [r["id"] for r in rows]

    def find_by_sender(self, from_email: str | None, before: datetime) -> list[dict[str, Any]]:
        if not from_email:
            return []
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, subject_norm, body_norm, received_at, dedup_hash
                FROM tickets
                WHERE from_email = %s AND received_at < %s
                ORDER BY received_at DESC LIMIT 20
                """,
                (from_email, before),
            ).fetchall()

    # --- write side ----------------------------------------------------------------

    def save_ticket(self, ticket: Ticket, state: TriageState | None = None) -> None:
        n = state.normalized if state else None
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tickets (
                    id, received_at, from_name, from_email, subject_raw, body_raw,
                    subject_norm, body_norm, language, text_quality, repairs,
                    dedup_hash, related_tickets, is_followup,
                    injection_suspected, injection_spans
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    subject_norm = EXCLUDED.subject_norm,
                    body_norm = EXCLUDED.body_norm,
                    language = EXCLUDED.language,
                    text_quality = EXCLUDED.text_quality,
                    repairs = EXCLUDED.repairs,
                    dedup_hash = EXCLUDED.dedup_hash,
                    related_tickets = EXCLUDED.related_tickets,
                    is_followup = EXCLUDED.is_followup,
                    injection_suspected = EXCLUDED.injection_suspected,
                    injection_spans = EXCLUDED.injection_spans
                """,
                (
                    ticket.id,
                    ticket.received_at,
                    ticket.from_name,
                    ticket.from_email,
                    ticket.subject,
                    ticket.body,
                    n.subject if n else None,
                    n.body if n else None,
                    n.language if n else None,
                    n.text_quality if n else None,
                    Jsonb(n.repairs if n else []),
                    n.dedup_hash if n else None,
                    Jsonb(n.related_tickets if n else []),
                    n.is_followup if n else False,
                    n.injection_suspected if n else False,
                    Jsonb([s.model_dump() for s in n.injection_spans] if n else []),
                ),
            )
            conn.commit()

    def save_run(self, state: TriageState, terminal_state: str = "completed") -> None:
        """Persist a completed run and its entire decision trail in ONE transaction.

        All-or-nothing: a half-written trail is worse than none, because it looks
        complete.
        """
        s = get_settings()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, ticket_id, graph_version, config_hash,
                                  started_at, ended_at, terminal_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    ended_at = EXCLUDED.ended_at,
                    terminal_state = EXCLUDED.terminal_state
                """,
                (
                    state.run_id,
                    state.ticket.id,
                    state.graph_version,
                    s.config_hash,
                    state.started_at,
                    state.ended_at or datetime.now(timezone.utc),
                    terminal_state,
                ),
            )

            for step in state.steps:
                conn.execute(
                    """
                    INSERT INTO agent_steps (
                        id, run_id, seq, agent, model_id, prompt_hash, input,
                        raw_output, parsed_output, latency_ms, input_tokens,
                        output_tokens, attempt, error_type, error_detail, created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id, seq, attempt) DO NOTHING
                    """,
                    (
                        step.id,
                        state.run_id,
                        step.seq,
                        step.agent,
                        step.model_id,
                        step.prompt_hash,
                        Jsonb(step.input),
                        step.raw_output,
                        Jsonb(step.parsed_output) if step.parsed_output else None,
                        step.latency_ms,
                        step.input_tokens,
                        step.output_tokens,
                        step.attempt,
                        step.error_type.value if step.error_type else None,
                        step.error_detail,
                        step.created_at,
                    ),
                )

            if state.policy:
                for i, fire in enumerate(state.policy.fires):
                    conn.execute(
                        """
                        INSERT INTO policy_events (id, run_id, ticket_id, seq, rule_id,
                                                   rule_version, triggered_by,
                                                   route_before, route_after, detail)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            _uuid(),
                            state.run_id,
                            state.ticket.id,
                            i,
                            fire.rule_id,
                            fire.rule_version,
                            fire.triggered_by,
                            fire.route_before.value,
                            fire.route_after.value,
                            fire.detail,
                        ),
                    )

            if state.final:
                c = state.confidence
                conn.execute(
                    """
                    INSERT INTO decisions (
                        run_id, ticket_id, proposed_route, policy_ceiling,
                        critic_verdict, final_route, escalation_queue, priority,
                        c_margin, c_crossmodel, c_selfreport, penalties, composite,
                        tau_auto, tau_draft, binding_constraint, rationale, contributors
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id) DO UPDATE SET
                        final_route = EXCLUDED.final_route,
                        binding_constraint = EXCLUDED.binding_constraint
                    """,
                    (
                        state.run_id,
                        state.ticket.id,
                        state.decision.route.value if state.decision else None,
                        state.policy.ceiling.value if state.policy else None,
                        state.critique.demote_to.value
                        if state.critique and state.critique.demote_to
                        else None,
                        state.final.route.value,
                        state.final.queue.value if state.final.queue else None,
                        state.final.priority.value,
                        c.c_margin if c else None,
                        c.c_crossmodel if c else None,
                        c.c_selfreport if c else None,
                        Jsonb(c.penalties if c else {}),
                        c.composite if c else None,
                        s.tau_auto,
                        s.tau_draft,
                        state.final.binding_constraint,
                        state.decision.rationale if state.decision else None,
                        Jsonb(state.final.contributors),
                    ),
                )

            if state.draft:
                conn.execute(
                    """
                    INSERT INTO drafts (run_id, ticket_id, template_id, subject, body,
                                        send, language, rationale, critique)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id) DO UPDATE SET
                        body = EXCLUDED.body, critique = EXCLUDED.critique
                    """,
                    (
                        state.run_id,
                        state.ticket.id,
                        state.draft.template_id,
                        state.draft.subject,
                        state.draft.body,
                        state.draft.send,
                        state.draft.language,
                        state.draft.rationale,
                        Jsonb(state.critique.model_dump(mode="json"))
                        if state.critique
                        else None,
                    ),
                )

            conn.commit()

    def dead_letter(
        self, ticket_id: str, run_id: str | None, stage: str, exc: BaseException, tb: str,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO dead_letters (id, ticket_id, run_id, stage,
                                          exception_type, traceback, state_snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (_uuid(), ticket_id, run_id, stage, type(exc).__name__, tb,
                 Jsonb(snapshot or {})),
            )
            conn.commit()

    # --- human-in-the-loop -----------------------------------------------------------

    def record_review(
        self, ticket_id: str, run_id: str, reviewer: str, action: str,
        draft_before: str | None = None, draft_after: str | None = None,
        reject_reason: str | None = None,
    ) -> str:
        rid = _uuid()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reviews (id, ticket_id, run_id, reviewer, action,
                                     draft_before, draft_after, reject_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (rid, ticket_id, run_id, reviewer, action,
                 draft_before, draft_after, reject_reason),
            )
            conn.commit()
        return rid

    # --- read side: queues and trail --------------------------------------------------

    def queue(self, route: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT d.ticket_id, d.final_route, d.composite, d.escalation_queue,
                       d.priority, d.binding_constraint, d.run_id,
                       t.subject_raw, t.from_name, t.from_email, t.received_at,
                       t.injection_suspected, t.is_followup, t.language,
                       dr.body AS draft_body, dr.subject AS draft_subject,
                       (SELECT COUNT(*) FROM reviews r WHERE r.run_id = d.run_id) AS review_count
                FROM decisions d
                JOIN tickets t ON t.id = d.ticket_id
                LEFT JOIN drafts dr ON dr.run_id = d.run_id
                WHERE d.final_route = %s::route
                ORDER BY
                  CASE d.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                                  WHEN 'P2' THEN 2 ELSE 3 END,
                  t.received_at DESC
                LIMIT %s
                """,
                (route, limit),
            ).fetchall()

    def trail(self, ticket_id: str) -> dict[str, Any]:
        """Everything needed to reconstruct one ticket's decision, in one call."""
        with self.connect() as conn:
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE id = %s", (ticket_id,)
            ).fetchone()
            if not ticket:
                return {}

            run = conn.execute(
                "SELECT * FROM runs WHERE ticket_id = %s ORDER BY started_at DESC LIMIT 1",
                (ticket_id,),
            ).fetchone()
            if not run:
                return {"ticket": ticket, "run": None}

            steps = conn.execute(
                "SELECT * FROM agent_steps WHERE run_id = %s ORDER BY seq, attempt",
                (run["id"],),
            ).fetchall()
            events = conn.execute(
                "SELECT * FROM policy_events WHERE run_id = %s ORDER BY seq",
                (run["id"],),
            ).fetchall()
            decision = conn.execute(
                "SELECT * FROM decisions WHERE run_id = %s", (run["id"],)
            ).fetchone()
            draft = conn.execute(
                "SELECT * FROM drafts WHERE run_id = %s", (run["id"],)
            ).fetchone()
            reviews = conn.execute(
                "SELECT * FROM reviews WHERE run_id = %s ORDER BY acted_at", (run["id"],)
            ).fetchall()

        return {
            "ticket": ticket,
            "run": run,
            "steps": steps,
            "policy_events": events,
            "decision": decision,
            "draft": draft,
            "reviews": reviews,
        }

    # --- monitoring queries -----------------------------------------------------------
    # These are the reason this is Postgres and not a JSON blob per ticket.

    def route_distribution(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT final_route, COUNT(*) AS n, AVG(composite) AS avg_conf "
                "FROM decisions GROUP BY final_route ORDER BY n DESC"
            ).fetchall()

    def rule_fire_counts(self) -> list[dict[str, Any]]:
        """A DROP here means classification drifted, not that risk went away.
        A quiet safety rule is a broken safety rule."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT rule_id, COUNT(*) AS n FROM policy_events "
                "GROUP BY rule_id ORDER BY n DESC"
            ).fetchall()

    def override_rate(self) -> list[dict[str, Any]]:
        """Human override rate -- the best ground-truth quality signal, and free."""
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT d.final_route,
                       COUNT(r.id) AS reviewed,
                       COUNT(*) FILTER (WHERE r.action = 'reject') AS rejected,
                       COUNT(*) FILTER (WHERE r.action = 'edit')   AS edited,
                       ROUND((COUNT(*) FILTER (WHERE r.action = 'reject'))::numeric
                             / NULLIF(COUNT(r.id), 0), 3) AS reject_rate
                FROM decisions d LEFT JOIN reviews r ON r.run_id = d.run_id
                GROUP BY d.final_route
                """
            ).fetchall()

    def calibration(self) -> list[dict[str, Any]]:
        """Predicted confidence vs. what humans actually did with it."""
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT width_bucket(d.composite, 0, 1, 10) AS conf_decile,
                       COUNT(*) AS n,
                       COUNT(r.id) AS reviewed,
                       COUNT(*) FILTER (WHERE r.action = 'reject') AS rejected
                FROM decisions d LEFT JOIN reviews r ON r.run_id = d.run_id
                GROUP BY 1 ORDER BY 1
                """
            ).fetchall()

    def failure_counts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT agent, error_type, COUNT(*) AS n FROM agent_steps "
                "WHERE error_type IS NOT NULL GROUP BY agent, error_type ORDER BY n DESC"
            ).fetchall()

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM tickets)                                    AS tickets,
                  (SELECT COUNT(*) FROM runs)                                       AS runs,
                  (SELECT COUNT(*) FROM agent_steps)                                AS steps,
                  (SELECT COUNT(*) FROM policy_events)                              AS policy_events,
                  (SELECT COUNT(*) FROM dead_letters)                               AS dead_letters,
                  (SELECT COUNT(*) FROM decisions WHERE final_route='auto_resolve') AS auto_resolved,
                  (SELECT COUNT(*) FROM reviews)                                    AS reviews
                """
            ).fetchone()
        return dict(row)
