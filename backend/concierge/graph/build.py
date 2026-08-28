"""The LangGraph state graph.

Nodes are agents; edges are plain-Python predicates. Control flow is never decided
by a model -- the client's hard requirement is *about* control flow, so control flow
has to be the deterministic part (ADR-001).

The runner guarantees the invariant that matters operationally: every ticket
reaches a terminal state. A ticket that throws is dead-lettered and escalated, never
dropped.
"""

from __future__ import annotations

import contextlib
import traceback
from datetime import UTC, datetime

from langgraph.graph import END, START, StateGraph

from concierge.agents.agents import Agents
from concierge.graph.nodes import Pipeline
from concierge.models import (
    AgentFailure,
    FailureType,
    FinalRoute,
    TriageState,
)
from concierge.policy.routes import EscalationQueue, Priority, Route


def build_graph(pipeline: Pipeline):
    """Wire the nodes into a StateGraph with conditional edges."""
    g = StateGraph(TriageState)

    g.add_node("normalize", pipeline.normalize_node)
    g.add_node("extract", pipeline.extract_node)
    g.add_node("classify", pipeline.classify_node)
    g.add_node("policy", pipeline.policy_node)
    g.add_node("decide", pipeline.decide_node)
    g.add_node("draft", pipeline.draft_node)
    g.add_node("critique", pipeline.critique_node)
    g.add_node("arbiter", pipeline.arbiter_node)

    g.add_edge(START, "normalize")
    g.add_edge("normalize", "extract")
    g.add_edge("extract", "classify")
    g.add_edge("classify", "policy")

    # --- conditional edge 1: skip the rest of the pipeline on a hard block -------
    # A hard-blocked ticket cannot be routed anywhere but escalate, so drafting and
    # critiquing would burn rate-limited calls that cannot change the outcome.
    def after_policy(state: TriageState) -> str:
        if state.policy is not None and state.policy.hard_blocked:
            return "decide"    # still records a proposal for the audit trail
        return "decide"

    g.add_conditional_edges("policy", after_policy, {"decide": "decide"})

    # --- conditional edge 2: only draft when a reply is actually wanted ------------
    def after_decide(state: TriageState) -> str:
        route = state.decision.route if state.decision else Route.ESCALATE
        return "arbiter" if route == Route.ESCALATE else "draft"

    g.add_conditional_edges(
        "decide", after_decide, {"draft": "draft", "arbiter": "arbiter"}
    )

    # --- conditional edge 3: only critique when a draft exists --------------------
    def after_draft(state: TriageState) -> str:
        if state.draft is None or not state.draft.body.strip():
            return "arbiter"
        return "critique"

    g.add_conditional_edges(
        "draft", after_draft, {"critique": "critique", "arbiter": "arbiter"}
    )

    g.add_edge("critique", "arbiter")
    g.add_edge("arbiter", END)

    return g.compile()


class TriageRunner:
    """Runs the graph for one ticket with a hard terminal-state guarantee."""

    def __init__(self, agents: Agents, lookup=None, repo=None):
        self.pipeline = Pipeline(agents, lookup=lookup)
        self.graph = build_graph(self.pipeline)
        self.repo = repo

    def run(self, ticket) -> TriageState:
        state = TriageState(ticket=ticket)
        try:
            result = self.graph.invoke(state)
            # LangGraph may return a dict; normalize back to the model.
            state = result if isinstance(result, TriageState) else TriageState(**result)
        except Exception as exc:
            state = self._dead_letter(state, exc)
        finally:
            state.ended_at = datetime.now(UTC)
            if state.final is None:
                # Belt and braces: no path may leave a ticket unrouted.
                state.final = FinalRoute(
                    route=Route.ESCALATE,
                    queue=EscalationQueue.SUPPORT,
                    priority=Priority.P1,
                    binding_constraint=(
                        "Pipeline did not produce a route; failed closed to escalation."
                    ),
                    contributors={"runner": "fail_closed"},
                )
        return state

    def _dead_letter(self, state: TriageState, exc: Exception) -> TriageState:
        tb = traceback.format_exc()
        state.failures.append(
            AgentFailure(
                agent="graph",
                error_type=FailureType.PROVIDER_ERROR,
                detail=f"{type(exc).__name__}: {exc}"[:500],
                demoted_to=Route.ESCALATE,
            )
        )
        state.final = FinalRoute(
            route=Route.ESCALATE,
            queue=EscalationQueue.SUPPORT,
            priority=Priority.P1,
            binding_constraint=(
                f"Unhandled {type(exc).__name__} in the pipeline; ticket dead-lettered "
                "and escalated rather than dropped."
            ),
            contributors={"dead_letter": "escalate"},
        )
        if self.repo is not None:
            # Never let audit logging kill the ticket it is trying to record.
            with contextlib.suppress(Exception):
                self.repo.dead_letter(
                    state.ticket.id, state.run_id, "graph", exc, tb,
                    {"steps": len(state.steps)},
                )
        return state
