"""Graph nodes and the per-agent failure-degradation policy.

Requirement 5: degrade safely toward escalation, never crash, never silently drop.
Each agent degrades DIFFERENTLY, and the differences are the interesting part:

  normalize   pure Python; if it throws that's a bug -> dead-letter
  extractor   continue with no extraction + a confidence penalty (classification
              is still possible from raw text -- degraded, not blocked)
  classifier  STOP -> escalate. Without labels the policy gate cannot evaluate the
              hard blocks at all; proceeding is exactly the unsafe-automation case
  decision    fall back to the policy ceiling, floored at draft_for_review
  drafter     auto_resolve -> escalate (two levels: you cannot auto-send a reply
              that was never written)
  critic      auto_resolve -> draft_for_review; a draft already bound for a human
              needs no critic, so demoting it further would trade real automation
              for zero safety gain. Degradation should be proportionate.
"""

from __future__ import annotations

from concierge.agents.agents import Agents
from concierge.confidence import composite as conf
from concierge.config import get_settings
from concierge.graph import templates
from concierge.models import (
    AgentFailure,
    Classification,
    ConfidenceBreakdown,
    DecisionProposal,
    Draft,
    Label,
    TriageState,
)
from concierge.normalize.node import normalize
from concierge.policy.arbiter import arbitrate
from concierge.policy.gate import PolicyGate
from concierge.policy.routes import Route, most_conservative

GATE = PolicyGate()


class Pipeline:
    """The triage pipeline. Nodes are methods; edges are the plain-Python
    conditionals in `run`. Control flow is never decided by a model."""

    def __init__(self, agents: Agents, lookup=None):
        self.agents = agents
        self.lookup = lookup
        self.s = get_settings()

    # -- node 0: normalize (deterministic) -----------------------------------------

    def normalize_node(self, state: TriageState) -> TriageState:
        state.normalized = normalize(state.ticket, self.lookup)
        return state

    # -- node 1: extract ------------------------------------------------------------

    def extract_node(self, state: TriageState) -> TriageState:
        out = self.agents.extract(state.ticket, state.normalized, state.next_seq())
        state.steps.extend(out.steps)
        if out.ok:
            state.extraction = out.value
        else:
            # Degraded, not blocked: the classifier can still work from raw text.
            # The `no_extraction` penalty makes the loss visible in the confidence.
            state.failures.append(
                AgentFailure(
                    agent="extractor", error_type=out.error_type,
                    detail=out.error_detail, attempts=out.attempts, demoted_to=None,
                )
            )
        return state

    # -- node 2: classify -----------------------------------------------------------

    def classify_node(self, state: TriageState) -> TriageState:
        out = self.agents.classify(state.normalized, state.next_seq())
        state.steps.extend(out.steps)

        if not out.ok:
            # Fail closed. No labels means the hard blocks cannot be evaluated.
            state.classification = None
            state.failures.append(
                AgentFailure(
                    agent="classifier", error_type=out.error_type,
                    detail=out.error_detail, attempts=out.attempts,
                    demoted_to=Route.ESCALATE,
                )
            )
            return state

        state.classification = out.value

        # Adaptive cross-model check: only when the margin is close enough to a
        # threshold to be worth the quota (free tier is ~15 RPM).
        c_cross = None
        margin = out.value.margin
        if conf.needs_crossmodel_check(
            margin, self.s.crossmodel_band_low, self.s.crossmodel_band_high
        ):
            second = self.agents.classify(
                state.normalized, state.next_seq(), model=self.s.model_smart
            )
            state.steps.extend(second.steps)
            if second.ok and second.value is not None:
                c_cross = conf.jaccard(
                    {l.value for l in out.value.above(0.5)},
                    {l.value for l in second.value.above(0.5)},
                )
            # A failed second opinion is not a failure of the ticket -- the primary
            # classification stands and the composite renormalizes without it.

        state.confidence = conf.compute(
            state.classification, state.extraction, state.normalized, c_cross
        )
        return state

    # -- node 3: policy gate (deterministic) -----------------------------------------

    def policy_node(self, state: TriageState) -> TriageState:
        state.policy = GATE.evaluate(
            state.classification, state.extraction, state.normalized
        )
        if state.confidence is None:
            state.confidence = conf.compute(
                state.classification, state.extraction, state.normalized, None
            )
        return state

    # -- node 4: decide ---------------------------------------------------------------

    def decide_node(self, state: TriageState) -> TriageState:
        # Confidence chooses WITHIN the ceiling the policy gate already set.
        threshold_route = self._route_from_confidence(state)

        # Skip the LLM entirely when the gate has already forced escalation: it
        # cannot change the outcome, and it costs a call we are rate-limited on.
        if state.policy and state.policy.ceiling == Route.ESCALATE:
            state.decision = DecisionProposal(
                route=Route.ESCALATE,
                rationale=(
                    "Policy gate forced escalation "
                    f"({', '.join(state.policy.rule_ids)}); decision agent skipped "
                    "because it cannot make the route less conservative."
                ),
                confidence_in_proposal=1.0,
            )
            return state

        c = state.confidence
        summary = (
            f"composite={c.composite:.2f} margin={c.c_margin:.2f} "
            f"penalties={list(c.penalties)}" if c else "unavailable"
        )
        out = self.agents.decide(
            state.normalized, state.extraction, state.classification, summary,
            state.next_seq(),
        )
        state.steps.extend(out.steps)

        if out.ok:
            # The agent's proposal and the threshold both count; take the stricter.
            state.decision = DecisionProposal(
                route=most_conservative(out.value.route, threshold_route),
                rationale=out.value.rationale,
                confidence_in_proposal=out.value.confidence_in_proposal,
            )
        else:
            state.failures.append(
                AgentFailure(
                    agent="decision", error_type=out.error_type,
                    detail=out.error_detail, attempts=out.attempts,
                    demoted_to=most_conservative(threshold_route, Route.DRAFT_FOR_REVIEW),
                )
            )
            state.decision = DecisionProposal(
                route=most_conservative(threshold_route, Route.DRAFT_FOR_REVIEW),
                rationale="Decision agent unavailable; fell back to the policy ceiling.",
            )
        return state

    def _route_from_confidence(self, state: TriageState) -> Route:
        """Confidence -> route, including the low-confidence special case."""
        c = state.confidence
        if c is None:
            return Route.ESCALATE
        if c.composite >= self.s.tau_auto:
            return Route.AUTO_RESOLVE
        if c.composite >= self.s.tau_draft:
            return Route.DRAFT_FOR_REVIEW
        return self._low_confidence_route(state)

    def _low_confidence_route(self, state: TriageState) -> Route:
        """ADR-005. Low confidence means "don't act on my guess", not "get a human".

        If the ticket is low-risk AND the reason for low confidence is missing
        information, the right action is an automated clarifying question.
        TCK-1009 ("help") is the case this exists for.
        """
        c, policy = state.confidence, state.policy
        if policy is None or policy.hard_blocked or policy.ceiling == Route.ESCALATE:
            return Route.ESCALATE
        if c and c.dominant_penalty == "signal_poverty":
            return Route.AUTO_RESOLVE   # -> clarification_request template
        return Route.ESCALATE

    # -- node 5: draft -----------------------------------------------------------------

    def draft_node(self, state: TriageState) -> TriageState:
        route = state.decision.route if state.decision else Route.ESCALATE
        if route == Route.ESCALATE:
            return state   # escalations carry no auto-generated response, by design

        template_id = self._template_for(state)
        if template_id is not None:
            subject, body, send = templates.render(
                template_id, state.normalized.subject, state.ticket.from_name
            )
            state.draft = Draft(
                template_id=template_id, subject=subject, body=body, send=send,
                language=state.normalized.language,
                rationale=f"Deterministic template '{template_id}'; no generation needed.",
            )
            return state

        out = self.agents.draft(
            state.normalized, state.extraction, state.classification, state.next_seq(),
            sender_name=state.ticket.from_name,
        )
        state.steps.extend(out.steps)
        if out.ok:
            state.draft = out.value
        else:
            # Cannot auto-send a reply that was never written. Two levels down.
            state.failures.append(
                AgentFailure(
                    agent="drafter", error_type=out.error_type,
                    detail=out.error_detail, attempts=out.attempts,
                    demoted_to=Route.ESCALATE,
                )
            )
        return state

    def _template_for(self, state: TriageState) -> str | None:
        """Pick a canned template when a fixed answer is correct."""
        c = state.confidence
        if c and c.dominant_penalty == "signal_poverty":
            return "clarification_request"

        labels = state.classification.above(0.5) if state.classification else []
        if len(labels) != 1:
            return None    # multi-intent needs a written reply, not a canned one

        return {
            Label.SPAM: "spam_close",
            Label.POSITIVE_FEEDBACK: "thanks_acknowledgement",
            Label.FEATURE_REQUEST: "feature_request_logged",
            Label.ACCOUNT_ACCESS: "password_reset_help",
            Label.BUG_REPORT: "bug_acknowledgement",
        }.get(labels[0])

    # -- node 6: critique (bonus) --------------------------------------------------------

    def critique_node(self, state: TriageState) -> TriageState:
        if state.draft is None or not state.draft.body.strip():
            return state   # nothing to review (escalation, or a no-reply close)

        inbound = state.decision.route if state.decision else Route.DRAFT_FOR_REVIEW
        out = self.agents.critique(
            state.normalized, state.draft, state.next_seq(),
            sender_name=state.ticket.from_name,
        )
        state.steps.extend(out.steps)

        if out.ok:
            state.critique = out.value
        else:
            # Proportionate degradation: a draft already going to a human is fine
            # without the critic. An auto-send is not.
            demote = Route.DRAFT_FOR_REVIEW if inbound == Route.AUTO_RESOLVE else None
            state.failures.append(
                AgentFailure(
                    agent="critic", error_type=out.error_type,
                    detail=out.error_detail, attempts=out.attempts, demoted_to=demote,
                )
            )
        return state

    # -- node 7: arbiter (deterministic) --------------------------------------------------

    def arbiter_node(self, state: TriageState) -> TriageState:
        state.final = arbitrate(state)
        # An escalation must not carry a draft: the brief is explicit that escalated
        # tickets get no auto-generated response.
        if state.final.route == Route.ESCALATE and state.draft is not None:
            state.draft = None
        return state
