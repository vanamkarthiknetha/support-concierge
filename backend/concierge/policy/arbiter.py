"""The arbiter: the single place a final route is decided.

Every other component -- policy gate, decision agent, critic, failure handler --
only ever *proposes*. This function takes the most conservative proposal and
returns it. That is the entire safety property, and it is ~40 lines so a reviewer
can verify it by reading rather than by trusting five prompts.

Read this file first.
"""

from __future__ import annotations

from concierge.models import (
    Critique,
    DecisionProposal,
    FinalRoute,
    PolicyResult,
    TriageState,
)
from concierge.policy.routes import (
    EscalationQueue,
    Priority,
    Route,
    assert_monotonic,
    most_conservative,
    most_urgent,
)


def arbitrate(state: TriageState) -> FinalRoute:
    """Collapse every component's opinion into one final route.

    Invariant: the result is never less severe than ANY contributing opinion.
    """
    contributors: dict[str, str] = {}

    policy: PolicyResult | None = state.policy
    decision: DecisionProposal | None = state.decision
    critique: Critique | None = state.critique

    opinions: list[Route] = []

    # 1. Policy ceiling -- the deterministic bound. Cannot be cleared.
    if policy is not None:
        opinions.append(policy.ceiling)
        contributors["policy_ceiling"] = policy.ceiling.value

    # 2. The decision agent's proposal. Advisory only.
    if decision is not None:
        opinions.append(decision.route)
        contributors["decision_agent"] = decision.route.value

    # 3. The critic may demote. `assert_monotonic` makes an attempted promotion
    #    a hard error rather than a silent downgrade of the safety property.
    if critique is not None and critique.demote_to is not None:
        base = decision.route if decision else Route.DRAFT_FOR_REVIEW
        assert_monotonic(base, critique.demote_to)
        opinions.append(critique.demote_to)
        contributors["critic"] = critique.demote_to.value

    # 4. Failure-induced demotions (agent timed out, returned garbage, etc.).
    for failure in state.failures:
        if failure.demoted_to is not None:
            opinions.append(failure.demoted_to)
            contributors[f"failure:{failure.agent}"] = failure.demoted_to.value

    # No opinions at all -> escalate. An absence of judgement is not evidence of safety.
    final_route = most_conservative(*opinions)

    priority = policy.priority if policy else Priority.P2
    if any(f.demoted_to for f in state.failures):
        priority = most_urgent(priority, Priority.P1)

    queue = _queue_for(final_route, policy)

    return FinalRoute(
        route=final_route,
        queue=queue,
        priority=priority,
        binding_constraint=_binding_constraint(final_route, contributors, state),
        contributors=contributors,
    )


def _queue_for(route: Route, policy: PolicyResult | None) -> EscalationQueue | None:
    if route != Route.ESCALATE:
        return None
    if policy and policy.queue:
        return policy.queue
    return EscalationQueue.SUPPORT


def _binding_constraint(
    route: Route, contributors: dict[str, str], state: TriageState
) -> str:
    """One sentence answering "why wasn't this auto-resolved?".

    This is the question a client actually asks, so the system answers it directly
    rather than making someone reconstruct it from the trail.
    """
    if route == Route.AUTO_RESOLVE:
        return "No constraint: low-risk category and confidence above the auto threshold."

    # Prefer the policy gate's own explanation -- it is the most specific.
    if state.policy and state.policy.fires:
        binding = [f for f in state.policy.fires if f.route_after == route]
        if binding:
            f = binding[0]
            return f"Policy rule '{f.rule_id}' fired ({f.triggered_by}): {f.detail}"

    for failure in state.failures:
        if failure.demoted_to == route:
            return (
                f"Agent '{failure.agent}' failed ({failure.error_type.value} after "
                f"{failure.attempts} attempt(s)); degraded toward human review."
            )

    if state.critique and state.critique.demote_to == route:
        issues = "; ".join(state.critique.issues) or state.critique.rationale
        return f"Review agent demoted the draft: {issues}"

    # Only blame confidence when confidence is actually what bound the route.
    from concierge.config import get_settings

    s = get_settings()
    conf = state.confidence
    if conf is not None:
        below_auto = conf.composite < s.tau_auto
        if below_auto:
            dom = conf.dominant_penalty
            base = (
                f"Composite confidence {conf.composite:.2f} is below the "
                f"auto-resolve threshold ({s.tau_auto})"
            )
            return f"{base}; dominant penalty: {dom}." if dom else base + "."

    if state.decision is not None and state.decision.route == route:
        rationale = state.decision.rationale.strip()
        return (
            f"Confidence was sufficient ({conf.composite:.2f}) but the decision agent "
            f"recommended {route.value}: {rationale}"
            if conf
            else f"Decision agent recommended {route.value}: {rationale}"
        )

    return f"Routed to {route.value} by: {', '.join(contributors) or 'default (fail-closed)'}."
