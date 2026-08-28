"""Full-pipeline tests with a stubbed LLM. No API key, no cost, no quota.

Two jobs:
1. Prove the terminal-state guarantee: 18 tickets in, 18 terminal states out, under
   every failure mode. Nothing is ever silently dropped.
2. Catch wiring errors (signature mismatches, bad enum coercion) that would
   otherwise only surface as a dead-lettered ticket during a live run.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from concierge.agents.agents import Agents
from concierge.graph.build import TriageRunner
from concierge.llm.client import AgentOutcome, LLMClient, RateLimiter
from concierge.llm.faults import FaultInjector, FaultSpec
from concierge.models import (
    AgentStep,
    FailureType,
    Ticket,
)
from concierge.policy.routes import Route

SAMPLES = json.loads(
    (Path(__file__).resolve().parents[1] / "data" / "sample_tickets.json").read_text(
        encoding="utf-8"
    )
)


def tickets() -> list[Ticket]:
    return [
        Ticket(
            id=t["id"],
            received_at=datetime.fromisoformat(t["received_at"].replace("Z", "+00:00")),
            from_name=t.get("from_name"),
            from_email=t.get("from_email"),
            subject=t.get("subject", ""),
            body=t.get("body", ""),
        )
        for t in SAMPLES
    ]


class StubLLM(LLMClient):
    """LLMClient that never touches the network.

    Subclasses the real client so the retry/breaker/fault plumbing is exercised;
    only the HTTP call is replaced.
    """

    def __init__(self, faults: FaultInjector | None = None):
        self.settings = __import__(
            "concierge.config", fromlist=["get_settings"]
        ).get_settings()
        self.faults = faults or FaultInjector()
        self.limiter = RateLimiter(100000)          # no throttling in tests
        from concierge.llm.client import CircuitBreaker

        self.breaker = CircuitBreaker(1.1, 10_000, 0.0)   # effectively never trips
        self.calls: list[str] = []

    def call(
        self, *, agent, model, system, user, schema, seq, temperature=0.0,
        fallback_model=None,
    ):
        self.calls.append(agent)

        fault = self.faults.check(agent)
        if fault is not None:
            return AgentOutcome(
                ok=False,
                error_type=fault.failure_type,
                error_detail=f"stub injected {fault.failure_type.value}",
                attempts=self.settings.llm_max_retries + 1,
                steps=[
                    AgentStep(
                        seq=seq, agent=agent, model_id=model, attempt=1,
                        error_type=fault.failure_type, error_detail="stub",
                    )
                ],
            )

        payload = _stub_payload(agent, user)
        value = schema.model_validate(payload)
        return AgentOutcome(
            value=value, ok=True, attempts=1,
            steps=[
                AgentStep(
                    seq=seq, agent=agent, model_id=model, attempt=1,
                    parsed_output=json.loads(value.model_dump_json()),
                )
            ],
        )


# Gold labels, so the stub classifier behaves like a CORRECT classifier. This lets
# these tests ask the right question: "given accurate labels, does the deterministic
# layer route safely?" Whether the real model produces accurate labels is a separate
# question, answered by the eval harness against the live API.
STUB_LABELS: dict[str, list[tuple[str, float]]] = {
    "TCK-1001": [("billing_question", 0.95)],
    "TCK-1002": [("bug_report", 0.97)],
    "TCK-1003": [("feature_request", 0.96)],
    "TCK-1004": [("account_access", 0.94)],
    "TCK-1005": [("billing_dispute", 0.93), ("subscription_change", 0.55)],
    "TCK-1006": [("bug_report", 0.88), ("billing_dispute", 0.82)],   # multi-intent
    "TCK-1007": [("account_access", 0.92)],
    "TCK-1008": [("spam", 0.98)],
    "TCK-1009": [("unknown", 0.80)],
    "TCK-1010": [("legal_request", 0.95), ("account_deletion", 0.90)],
    "TCK-1011": [("refund_request", 0.96)],
    "TCK-1012": [("account_deletion", 0.97)],
    "TCK-1013": [("refund_request", 0.90)],
    "TCK-1014": [("bug_report", 0.95)],
    "TCK-1015": [("security_report", 0.97)],
    "TCK-1016": [("positive_feedback", 0.98)],
    "TCK-1017": [("bug_report", 0.85)],
    "TCK-1018": [("billing_question", 0.88), ("subscription_change", 0.70)],
}

STUB_ACTIONS: dict[str, list[str]] = {
    "TCK-1001": ["explanation"],
    "TCK-1005": ["refund"],
    "TCK-1006": ["explanation", "refund"],
    "TCK-1010": ["data_deletion"],
    "TCK-1011": ["refund"],
    "TCK-1012": ["account_deletion"],
    "TCK-1013": ["refund"],
    "TCK-1016": ["none"],
    "TCK-1018": ["explanation", "discount"],   # the one-word risk
}


# The prompt carries only subject + body (never the ticket id), so identify the
# ticket by a distinctive phrase. TCK-1013 is keyed off the customer's real request
# rather than the injected payload, because normalization quarantines that payload
# before any agent sees it -- which is exactly the behaviour under test.
STUB_FINGERPRINTS: list[tuple[str, str]] = [
    ("$12 higher", "TCK-1001"),
    ("export to csv", "TCK-1002"),
    ("dark mode", "TCK-1003"),
    ("password reset email", "TCK-1004"),
    ("third month in a row", "TCK-1005"),
    ("dashboard keeps timing out", "TCK-1006"),
    ("iniciar sesi", "TCK-1007"),
    ("followers", "TCK-1008"),
    ("article 17", "TCK-1010"),
    ("refund the annual payment", "TCK-1011"),
    ("permanently delete my account", "TCK-1012"),
    ("refund for last month", "TCK-1013"),
    ("reported this yesterday", "TCK-1014"),
    ("account_id", "TCK-1015"),
    ("wanted to say thank", "TCK-1016"),
    ("report page keeps showing", "TCK-1017"),
    ("pricing went up", "TCK-1018"),
]


def _ticket_id_from(user: str) -> str | None:
    low = user.lower()
    for phrase, tid in STUB_FINGERPRINTS:
        if phrase in low:
            return tid
    # TCK-1009 is "it's broken" / "help" -- too short for a distinctive phrase.
    if "broken" in low and len(low) < 400:
        return "TCK-1009"
    return None


def _stub_payload(agent: str, user: str) -> dict:
    """Responses shaped like a correct model's, keyed off the sample tickets."""
    tid = _ticket_id_from(user)

    if agent == "extractor":
        actions = STUB_ACTIONS.get(tid or "", ["explanation"])
        return {
            "requested_actions": actions,
            "sentiment": "angry" if tid == "TCK-1005" else "neutral",
            "churn_risk": tid in {"TCK-1005", "TCK-1018"},
            "deadline_asserted": "30 days" if tid == "TCK-1010" else None,
            "summary": "stub",
        }
    if agent == "classifier":
        pairs = STUB_LABELS.get(tid or "") or [("bug_report", 0.9)]
        return {
            "labels": [{"label": lbl, "score": sc} for lbl, sc in pairs],
            "reasoning": "stub",
        }
    if agent == "decision":
        return {"route": "auto_resolve", "rationale": "stub", "confidence_in_proposal": 0.9}
    if agent == "drafter":
        return {"template_id": None, "subject": "Re: stub", "body": "Stub reply body.",
                "send": True, "rationale": "stub"}
    if agent == "critic":
        return {"approved": True, "issues": [], "demote_to": None, "rationale": "stub"}
    raise AssertionError(f"unexpected agent {agent}")


def runner(faults: FaultInjector | None = None) -> TriageRunner:
    return TriageRunner(Agents(StubLLM(faults)))


# --------------------------------------------------------------------------------
# The terminal-state guarantee
# --------------------------------------------------------------------------------


def test_all_18_tickets_reach_a_terminal_state():
    r = runner()
    states = [r.run(t) for t in tickets()]
    assert len(states) == 18
    for st in states:
        assert st.final is not None, f"{st.ticket.id} produced no route"
        assert st.final.route in tuple(Route)
        assert st.final.binding_constraint, f"{st.ticket.id} has no explanation"


def test_no_wiring_errors_on_any_ticket():
    """A dead-lettered ticket here means a signature/plumbing bug, not a policy call."""
    r = runner()
    dead = [
        st.ticket.id
        for st in (r.run(t) for t in tickets())
        if any("Unhandled" in (st.final.binding_constraint or "") for _ in [0])
        and "Unhandled" in st.final.binding_constraint
    ]
    assert dead == [], f"tickets dead-lettered by an internal error: {dead}"


@pytest.mark.parametrize(
    "agent",
    ["extractor", "classifier", "decision", "drafter", "critic"],
)
@pytest.mark.parametrize("mode", ["timeout", "malformed", "error_500", "refusal"])
def test_every_ticket_survives_every_agent_failure(agent, mode):
    """Requirement 5, exhaustively: 5 agents x 4 failure modes, all 18 tickets."""
    r = runner(FaultInjector([FaultSpec(agent=agent, mode=mode, rate=1.0)]))
    for t in tickets():
        st = r.run(t)
        assert st.final is not None, f"{t.id} lost under {agent}/{mode}"
        assert st.final.route in tuple(Route)


def test_classifier_failure_escalates_everything():
    """Without labels the hard blocks cannot be evaluated, so nothing may automate."""
    r = runner(FaultInjector([FaultSpec(agent="classifier", mode="timeout", rate=1.0)]))
    routes = {r.run(t).final.route for t in tickets()}
    assert routes == {Route.ESCALATE}


def test_auto_resolve_always_has_a_reply_to_send():
    """The real invariant: you cannot auto-send a reply that does not exist.

    Note this is NOT "drafter failure always blocks auto-resolve". Tickets answered
    by a deterministic template never call the drafter, so its unavailability is
    irrelevant to them -- and auto-resolving those is correct. Asserting the
    stronger claim would have been asserting a bug.
    """
    r = runner(FaultInjector([FaultSpec(agent="drafter", mode="error_500", rate=1.0)]))
    for t in tickets():
        st = r.run(t)
        if st.final.route == Route.AUTO_RESOLVE:
            assert st.draft is not None, f"{t.id} auto-resolved with no draft at all"
            # `send=False` (spam close) is a legitimate empty body.
            assert st.draft.body.strip() or not st.draft.send, (
                f"{t.id} auto-resolved with an empty reply it intends to send"
            )


def test_llm_drafted_replies_are_blocked_when_the_drafter_fails():
    """For tickets that DO need generation, a drafter outage must demote them."""
    r = runner(FaultInjector([FaultSpec(agent="drafter", mode="error_500", rate=1.0)]))
    generated = [
        st for st in (r.run(t) for t in tickets())
        if any(f.agent == "drafter" for f in st.failures)
    ]
    assert generated, "no ticket exercised the LLM drafting path"
    for st in generated:
        assert st.final.route == Route.ESCALATE, (
            f"{st.ticket.id} needed a generated reply, the drafter failed, "
            f"but it routed to {st.final.route.value}"
        )


def test_total_provider_outage_escalates_everything():
    r = runner(FaultInjector([FaultSpec(agent="*", mode="error_500", rate=1.0)]))
    for t in tickets():
        st = r.run(t)
        assert st.final.route == Route.ESCALATE


def test_extractor_failure_degrades_but_does_not_block():
    """The classifier can still work from raw text -- degraded, not blocked."""
    r = runner(FaultInjector([FaultSpec(agent="extractor", mode="timeout", rate=1.0)]))
    st = r.run(tickets()[1])   # TCK-1002, a clean bug report
    assert st.final is not None
    assert any(f.agent == "extractor" for f in st.failures)
    assert st.confidence is not None
    assert "no_extraction" in st.confidence.penalties


def test_failures_are_recorded_as_steps_not_swallowed():
    r = runner(FaultInjector([FaultSpec(agent="classifier", mode="timeout", rate=1.0)]))
    st = r.run(tickets()[0])
    errored = [s for s in st.steps if s.error_type is not None]
    assert errored, "a failed attempt left no audit row"
    assert errored[0].error_type is FailureType.TIMEOUT


# --------------------------------------------------------------------------------
# Escalations carry no draft
# --------------------------------------------------------------------------------


def test_escalated_tickets_never_carry_a_draft():
    """The brief is explicit: escalation means no auto-generated response."""
    r = runner()
    for t in tickets():
        st = r.run(t)
        if st.final.route == Route.ESCALATE:
            assert st.draft is None, f"{t.id} escalated but carried a draft"


def test_hard_blocked_sample_tickets_never_auto_resolve():
    """The 8 tickets that must never be automated, end to end through the pipeline."""
    must_not = {
        "TCK-1005", "TCK-1006", "TCK-1010", "TCK-1011",
        "TCK-1012", "TCK-1013", "TCK-1015", "TCK-1018",
    }
    r = runner()
    for t in tickets():
        if t.id in must_not:
            st = r.run(t)
            assert st.final.route != Route.AUTO_RESOLVE, (
                f"{t.id} was auto-resolved; this is a safety violation"
            )
