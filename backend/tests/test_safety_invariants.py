"""The tests that encode the client's hard requirement as executable assertions.

If any test in this file fails, the system is unsafe to ship regardless of how
well it scores on routing accuracy.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from concierge.models import (
    AgentFailure,
    Classification,
    Critique,
    DecisionProposal,
    Extraction,
    FailureType,
    Label,
    Normalized,
    RequestedAction,
    ScoredLabel,
    Ticket,
    TriageState,
)
from concierge.policy.arbiter import arbitrate
from concierge.policy.gate import HARD_BLOCK_LABELS, PolicyGate
from concierge.policy.routes import (
    MonotonicityViolation,
    Route,
    assert_monotonic,
    most_conservative,
)


def _ticket(tid: str = "TCK-TEST") -> Ticket:
    return Ticket(
        id=tid,
        received_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        from_email="x@example.com",
        subject="s",
        body="b",
    )


def _state(**kw) -> TriageState:
    return TriageState(ticket=_ticket(), **kw)


def _norm(**kw) -> Normalized:
    base = dict(subject="s", body="b", language="en", token_estimate=50)
    base.update(kw)
    return Normalized(**base)


# --------------------------------------------------------------------------------
# 1. The monotonicity invariant -- the load-bearing property
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "routes",
    list(itertools.product(list(Route), repeat=3)),
)
def test_most_conservative_never_less_severe_than_any_input(routes):
    """Exhaustive over every 3-way combination of routes."""
    result = most_conservative(*routes)
    for r in routes:
        assert result.severity >= r.severity


def test_most_conservative_ignores_none_but_not_severity():
    assert most_conservative(None, Route.AUTO_RESOLVE, None) == Route.AUTO_RESOLVE
    assert most_conservative(Route.AUTO_RESOLVE, Route.ESCALATE) == Route.ESCALATE
    assert most_conservative(Route.ESCALATE, Route.AUTO_RESOLVE) == Route.ESCALATE


def test_no_opinions_fails_closed():
    """An absence of judgement is not evidence that a ticket is safe."""
    assert most_conservative() == Route.ESCALATE
    assert most_conservative(None, None) == Route.ESCALATE


def test_promotion_raises():
    assert_monotonic(Route.AUTO_RESOLVE, Route.ESCALATE)      # demotion: fine
    assert_monotonic(Route.DRAFT_FOR_REVIEW, Route.ESCALATE)  # demotion: fine
    assert_monotonic(Route.ESCALATE, Route.ESCALATE)          # no change: fine
    with pytest.raises(MonotonicityViolation):
        assert_monotonic(Route.ESCALATE, Route.AUTO_RESOLVE)
    with pytest.raises(MonotonicityViolation):
        assert_monotonic(Route.DRAFT_FOR_REVIEW, Route.AUTO_RESOLVE)


# --------------------------------------------------------------------------------
# 2. Hard blocks never auto-resolve -- at ANY confidence
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(HARD_BLOCK_LABELS, key=lambda l: l.value))
@pytest.mark.parametrize("score", [round(x * 0.05, 2) for x in range(11, 21)])
def test_hard_block_labels_never_auto_resolve_at_any_confidence(label, score):
    """The client's requirement, stated exactly: regardless of confidence.

    Swept across every hard-block category at every score from 0.55 to 1.00.
    """
    gate = PolicyGate()
    result = gate.evaluate(
        classification=Classification(labels=[ScoredLabel(label=label, score=score)]),
        extraction=Extraction(),
        normalized=_norm(),
    )
    assert result.ceiling == Route.ESCALATE, f"{label.value} @ {score} escaped the gate"
    assert result.hard_blocked is True


@pytest.mark.parametrize("label", sorted(HARD_BLOCK_LABELS, key=lambda l: l.value))
def test_hard_block_survives_a_confident_decision_agent(label):
    """Even if the decision agent insists on auto-resolving, the arbiter refuses."""
    gate = PolicyGate()
    policy = gate.evaluate(
        Classification(labels=[ScoredLabel(label=label, score=0.99)]),
        Extraction(),
        _norm(),
    )
    state = _state(
        policy=policy,
        decision=DecisionProposal(
            route=Route.AUTO_RESOLVE,
            rationale="I am extremely confident this is routine",
            confidence_in_proposal=1.0,
        ),
    )
    assert arbitrate(state).route == Route.ESCALATE


def test_money_movement_blocks_even_without_a_blocked_label():
    """TCK-1018: a billing *question* whose real ask is a discount.

    The label is benign; the requested action is not. Catching this is the whole
    reason the extractor emits `requested_actions`.
    """
    gate = PolicyGate()
    result = gate.evaluate(
        Classification(labels=[ScoredLabel(label=Label.BILLING_QUESTION, score=0.9)]),
        Extraction(requested_actions=[RequestedAction.DISCOUNT]),
        _norm(),
    )
    assert result.ceiling == Route.ESCALATE
    assert "money_movement_requested" in result.rule_ids


def test_explanation_request_does_not_block():
    """TCK-1001: same category, but nothing moves. Must NOT be a hard block.

    The counterpart to the test above -- together they prove the gate keys on
    'is a monetary state change requested?', not on 'does it mention billing?'.
    """
    gate = PolicyGate()
    result = gate.evaluate(
        Classification(labels=[ScoredLabel(label=Label.BILLING_QUESTION, score=0.9)]),
        Extraction(requested_actions=[RequestedAction.EXPLANATION]),
        _norm(),
    )
    assert result.hard_blocked is False
    assert result.ceiling == Route.AUTO_RESOLVE  # confidence decides from here


# --------------------------------------------------------------------------------
# 3. Injection
# --------------------------------------------------------------------------------


def test_injection_forces_escalation():
    gate = PolicyGate()
    result = gate.evaluate(
        Classification(labels=[ScoredLabel(label=Label.BUG_REPORT, score=0.99)]),
        Extraction(),
        _norm(injection_suspected=True),
    )
    assert result.ceiling == Route.ESCALATE
    assert "injection_suspected" in result.rule_ids


def test_tck1013_route_is_over_determined():
    """Delete every injection defence and `refund_request` alone still blocks it.

    The attack has to beat all four layers, not one. This test asserts the
    over-determination directly by disabling the injection signal.
    """
    gate = PolicyGate()
    result = gate.evaluate(
        Classification(labels=[ScoredLabel(label=Label.REFUND_REQUEST, score=0.9)]),
        Extraction(requested_actions=[RequestedAction.REFUND]),
        _norm(injection_suspected=False),  # injection defence disabled
    )
    assert result.ceiling == Route.ESCALATE
    assert "hard_block.refund_request" in result.rule_ids
    assert "money_movement_requested" in result.rule_ids  # and independently again


# --------------------------------------------------------------------------------
# 4. Failure degradation
# --------------------------------------------------------------------------------


def test_missing_classification_fails_closed():
    """Without labels the hard blocks cannot be evaluated at all."""
    gate = PolicyGate()
    for cls in (None, Classification(labels=[])):
        result = gate.evaluate(cls, Extraction(), _norm())
        assert result.ceiling == Route.ESCALATE
        assert "no_classification" in result.rule_ids


def test_agent_failure_demotes_final_route():
    state = _state(
        policy=PolicyGate().evaluate(
            Classification(labels=[ScoredLabel(label=Label.BUG_REPORT, score=0.95)]),
            Extraction(),
            _norm(),
        ),
        decision=DecisionProposal(route=Route.AUTO_RESOLVE),
        failures=[
            AgentFailure(
                agent="drafter",
                error_type=FailureType.TIMEOUT,
                attempts=3,
                demoted_to=Route.ESCALATE,
            )
        ],
    )
    final = arbitrate(state)
    assert final.route == Route.ESCALATE
    assert "failure:drafter" in final.contributors


def test_critic_can_demote_but_not_promote():
    base = _state(
        policy=PolicyGate().evaluate(
            Classification(labels=[ScoredLabel(label=Label.BUG_REPORT, score=0.95)]),
            Extraction(),
            _norm(),
        ),
        decision=DecisionProposal(route=Route.AUTO_RESOLVE),
    )

    base.critique = Critique(approved=False, demote_to=Route.DRAFT_FOR_REVIEW)
    assert arbitrate(base).route == Route.DRAFT_FOR_REVIEW

    # A critic trying to promote an escalation is a hard error, never a silent downgrade.
    promoted = _state(
        policy=PolicyGate().evaluate(
            Classification(labels=[ScoredLabel(label=Label.REFUND_REQUEST, score=0.9)]),
            Extraction(),
            _norm(),
        ),
        decision=DecisionProposal(route=Route.ESCALATE),
        critique=Critique(approved=True, demote_to=Route.AUTO_RESOLVE),
    )
    with pytest.raises(MonotonicityViolation):
        arbitrate(promoted)


# --------------------------------------------------------------------------------
# 5. Escalation targeting
# --------------------------------------------------------------------------------


def test_security_wins_queue_precedence():
    """TCK-1015 must not land in general support because billing is also mentioned."""
    gate = PolicyGate()
    result = gate.evaluate(
        Classification(
            labels=[
                ScoredLabel(label=Label.SECURITY_REPORT, score=0.9),
                ScoredLabel(label=Label.BILLING_QUESTION, score=0.6),
            ]
        ),
        Extraction(),
        _norm(),
    )
    assert result.queue.value == "security"
    assert result.priority.value == "P0"


def test_gdpr_fires_two_independent_rules():
    """TCK-1010: legal AND deletion. Both recorded, not just the first match."""
    gate = PolicyGate()
    result = gate.evaluate(
        Classification(
            labels=[
                ScoredLabel(label=Label.LEGAL_REQUEST, score=0.9),
                ScoredLabel(label=Label.ACCOUNT_DELETION, score=0.85),
            ]
        ),
        Extraction(requested_actions=[RequestedAction.DATA_DELETION]),
        _norm(),
    )
    assert "hard_block.legal_request" in result.rule_ids
    assert "hard_block.account_deletion" in result.rule_ids
    assert "data_deletion_requested" in result.rule_ids
    assert result.queue.value == "legal"
