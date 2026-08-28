"""Domain models threaded through the triage graph.

Design rule: the state object is APPEND-ONLY. No node overwrites another node's
fields. That is what makes the decision trail reconstructable after the fact --
if the drafter could rewrite the classification, the audit log would be fiction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from concierge.policy.routes import EscalationQueue, Priority, Route


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid.uuid4())


# --- taxonomy --------------------------------------------------------------------


class Label(str, Enum):
    """Intent taxonomy. Multi-label: a ticket may carry several (see TCK-1006)."""

    BILLING_QUESTION = "billing_question"        # asks about charges; no money moves
    BILLING_DISPUTE = "billing_dispute"          # contests a charge
    REFUND_REQUEST = "refund_request"
    SUBSCRIPTION_CHANGE = "subscription_change"  # upgrade/downgrade/cancel/discount
    ACCOUNT_ACCESS = "account_access"            # can't log in, reset issues
    ACCOUNT_DELETION = "account_deletion"
    SECURITY_REPORT = "security_report"
    LEGAL_REQUEST = "legal_request"              # GDPR, subpoena, legal threat
    BUG_REPORT = "bug_report"
    FEATURE_REQUEST = "feature_request"
    SPAM = "spam"
    POSITIVE_FEEDBACK = "positive_feedback"
    UNKNOWN = "unknown"


class RequestedAction(str, Enum):
    """What the sender wants DONE -- distinct from what they're talking ABOUT.

    This distinction is load-bearing. TCK-1018 is a billing *question* whose real
    ask ("is there any flexibility?") is a commercial concession. Classifying by
    topic alone misses it entirely; classifying by requested action catches it.
    """

    EXPLANATION = "explanation"              # tell me why -- no state change
    REFUND = "refund"
    CREDIT = "credit"
    DISCOUNT = "discount"                    # TCK-1018's "flexibility"
    PLAN_CHANGE = "plan_change"
    CANCELLATION = "cancellation"
    ACCOUNT_DELETION = "account_deletion"
    DATA_DELETION = "data_deletion"
    DATA_EXPORT = "data_export"
    TECHNICAL_FIX = "technical_fix"
    ACCESS_RESTORE = "access_restore"
    NONE = "none"                            # TCK-1016: "no action needed"

    @property
    def moves_money(self) -> bool:
        """Does granting this change the customer's monetary state?

        The client's hard requirement is about money moving, not about the word
        'billing' appearing. This property is where that distinction lives.
        """
        return self in _MONEY_MOVING


_MONEY_MOVING = frozenset(
    {
        RequestedAction.REFUND,
        RequestedAction.CREDIT,
        RequestedAction.DISCOUNT,
        RequestedAction.PLAN_CHANGE,
        RequestedAction.CANCELLATION,
    }
)


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    ANGRY = "angry"


# --- input ------------------------------------------------------------------------


class Ticket(BaseModel):
    """A ticket exactly as received. Never mutated."""

    model_config = ConfigDict(frozen=True)

    id: str
    received_at: datetime
    from_name: str | None = None
    from_email: str | None = None
    subject: str = ""
    body: str = ""


# --- deterministic pre-processing --------------------------------------------------


class InjectionSpan(BaseModel):
    """A span of text that looks like an instruction aimed at the system.

    Quarantined here and never re-inserted into a downstream prompt. See ADR-007.
    """

    start: int
    end: int
    text: str
    pattern: str  # which detector fired -- recorded so false-positive rate is measurable


class Normalized(BaseModel):
    """Output of the deterministic normalization node. No LLM involved."""

    subject: str
    body: str
    language: str = "en"
    text_quality: float = Field(1.0, ge=0.0, le=1.0)
    repairs: list[str] = Field(default_factory=list)  # what normalization actually did
    token_estimate: int = 0

    injection_suspected: bool = False
    injection_spans: list[InjectionSpan] = Field(default_factory=list)

    dedup_hash: str = ""
    related_tickets: list[str] = Field(default_factory=list)
    is_followup: bool = False


# --- agent outputs ------------------------------------------------------------------


class Extraction(BaseModel):
    """Structured fields pulled from the ticket by the extractor agent."""

    account_identifiers: list[str] = Field(default_factory=list)
    product_area: str | None = None
    requested_actions: list[RequestedAction] = Field(default_factory=list)
    mentioned_amounts: list[str] = Field(default_factory=list)
    mentioned_dates: list[str] = Field(default_factory=list)
    sentiment: Sentiment = Sentiment.NEUTRAL
    urgency_markers: list[str] = Field(default_factory=list)
    churn_risk: bool = False
    deadline_asserted: str | None = None   # TCK-1010's "within 30 days"
    summary: str = ""

    @property
    def requests_money_movement(self) -> bool:
        return any(a.moves_money for a in self.requested_actions)


class ScoredLabel(BaseModel):
    label: Label
    score: float = Field(ge=0.0, le=1.0)


class Classification(BaseModel):
    """Multi-label classification with per-label scores.

    Multi-label is not a nicety: single-label silently drops TCK-1006's billing half,
    which is exactly how a double-charge complaint gets auto-closed as a bug report.
    """

    labels: list[ScoredLabel] = Field(default_factory=list)
    reasoning: str = ""

    def above(self, threshold: float = 0.5) -> list[Label]:
        return [sl.label for sl in self.labels if sl.score >= threshold]

    @property
    def ranked(self) -> list[ScoredLabel]:
        return sorted(self.labels, key=lambda sl: sl.score, reverse=True)

    @property
    def top(self) -> ScoredLabel | None:
        r = self.ranked
        return r[0] if r else None

    @property
    def margin(self) -> float:
        """top-1 minus top-2 score.

        The primary confidence signal (see .claude/context/phase0-findings.md):
        measured 0.96 on a clean bug report and 0.05 on a genuinely multi-intent
        ticket. Measures decision ambiguity, which is what we actually care about.
        """
        r = self.ranked
        if not r:
            return 0.0
        if len(r) == 1:
            return r[0].score
        return max(0.0, r[0].score - r[1].score)


class ConfidenceBreakdown(BaseModel):
    """Every number behind the routing decision, persisted for audit."""

    c_margin: float = 0.0
    c_crossmodel: float | None = None    # None when the adaptive check didn't run
    c_selfreport: float = 0.0
    penalties: dict[str, float] = Field(default_factory=dict)
    composite: float = 0.0
    notes: list[str] = Field(default_factory=list)

    @property
    def penalty_total(self) -> float:
        return sum(self.penalties.values())

    @property
    def dominant_penalty(self) -> str | None:
        if not self.penalties:
            return None
        return max(self.penalties.items(), key=lambda kv: kv[1])[0]


class PolicyRuleFire(BaseModel):
    """One policy rule firing. Every fire is recorded, not just the first match."""

    rule_id: str
    rule_version: str = "1"
    triggered_by: str
    route_before: Route
    route_after: Route
    detail: str = ""


class PolicyResult(BaseModel):
    """The deterministic ceiling. No LLM output can clear this."""

    ceiling: Route
    fires: list[PolicyRuleFire] = Field(default_factory=list)
    queue: EscalationQueue | None = None
    priority: Priority = Priority.P2
    hard_blocked: bool = False

    @property
    def rule_ids(self) -> list[str]:
        return [f.rule_id for f in self.fires]


class DecisionProposal(BaseModel):
    """What the decision agent PROPOSES. It does not decide -- the arbiter does."""

    route: Route
    rationale: str = ""
    confidence_in_proposal: float = 0.0


class Draft(BaseModel):
    template_id: str | None = None
    subject: str = ""
    body: str = ""
    send: bool = True          # False = close without replying (TCK-1008 spam)
    language: str = "en"
    rationale: str = ""


class Critique(BaseModel):
    """Reflection pass over a draft. May demote the route; may never promote it."""

    approved: bool
    issues: list[str] = Field(default_factory=list)
    promises_forbidden_action: bool = False
    asserts_unsupported_facts: bool = False
    language_mismatch: bool = False
    tone_appropriate: bool = True
    demote_to: Route | None = None
    rationale: str = ""


# --- failure tracking ----------------------------------------------------------------


class FailureType(str, Enum):
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    REFUSED = "refused"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    CIRCUIT_OPEN = "circuit_open"


class AgentFailure(BaseModel):
    agent: str
    error_type: FailureType
    detail: str = ""
    attempts: int = 1
    demoted_to: Route | None = None


class AgentStep(BaseModel):
    """One agent invocation, including failed attempts. Persisted verbatim."""

    id: str = Field(default_factory=_uuid)
    seq: int
    agent: str
    model_id: str | None = None
    prompt_hash: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    raw_output: str | None = None
    parsed_output: dict[str, Any] | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    attempt: int = 1
    error_type: FailureType | None = None
    error_detail: str | None = None
    created_at: datetime = Field(default_factory=_now)


# --- final ----------------------------------------------------------------------------


class FinalRoute(BaseModel):
    route: Route
    queue: EscalationQueue | None = None
    priority: Priority = Priority.P2
    binding_constraint: str = ""   # the one-sentence "why not auto-resolved?" answer
    contributors: dict[str, str] = Field(default_factory=dict)


class TriageState(BaseModel):
    """State threaded through the graph. Append-only per node."""

    ticket: Ticket
    run_id: str = Field(default_factory=_uuid)
    graph_version: str = "1.0.0"

    normalized: Normalized | None = None
    extraction: Extraction | None = None
    classification: Classification | None = None
    confidence: ConfidenceBreakdown | None = None
    policy: PolicyResult | None = None
    decision: DecisionProposal | None = None
    draft: Draft | None = None
    critique: Critique | None = None
    final: FinalRoute | None = None

    failures: list[AgentFailure] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)

    started_at: datetime = Field(default_factory=_now)
    ended_at: datetime | None = None

    def next_seq(self) -> int:
        return len(self.steps)

    @property
    def failure_routes(self) -> list[Route]:
        return [f.demoted_to for f in self.failures if f.demoted_to is not None]
