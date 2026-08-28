"""The five LLM agents.

Each is a thin function: build a prompt, call the LLM through the wrapper, return
an AgentOutcome. All the judgement about what to DO with a failure lives in the
graph nodes, not here -- these just report what happened.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from concierge.agents.prompts import (
    CLASSIFIER_SYSTEM,
    TAXONOMY,
    CRITIC_SYSTEM,
    DECISION_SYSTEM,
    DRAFTER_SYSTEM,
    EXTRACTOR_SYSTEM,
    wrap_ticket,
)
from concierge.config import get_settings
from concierge.llm.client import AgentOutcome, LLMClient
from concierge.models import (
    Classification,
    Critique,
    DecisionProposal,
    Draft,
    Extraction,
    Normalized,
    Ticket,
)


# --- response schemas -----------------------------------------------------------
# Kept separate from the domain models: the wire schema is what the model must
# produce, and it should be as small as possible. Enums are sent as plain strings
# because a constrained enum in the response schema makes flash-lite brittle.


class ExtractionOut(BaseModel):
    account_identifiers: list[str] = Field(default_factory=list)
    product_area: str | None = None
    requested_actions: list[str] = Field(default_factory=list)
    mentioned_amounts: list[str] = Field(default_factory=list)
    mentioned_dates: list[str] = Field(default_factory=list)
    sentiment: str = "neutral"
    urgency_markers: list[str] = Field(default_factory=list)
    churn_risk: bool = False
    deadline_asserted: str | None = None
    summary: str = ""


class ScoredLabelOut(BaseModel):
    # Gemini uses schema field descriptions as generation guidance. Dropping these
    # regressed the classifier to a single label at score 1.0 on every ticket,
    # which flattened the margin signal to a constant. They are load-bearing.
    label: str = Field(description=f"one of: {TAXONOMY}")
    score: float = Field(description="0-1: how strongly THIS label applies to the ticket")


class ClassificationOut(BaseModel):
    labels: list[ScoredLabelOut] = Field(
        default_factory=list,
        description=(
            "EVERY applicable label, ranked by score descending. A ticket raising "
            "two distinct concerns must return both. Include partially-applicable "
            "labels with lower scores rather than omitting them."
        ),
    )
    reasoning: str = Field(default="", description="one sentence naming the evidence used")


class DecisionOut(BaseModel):
    route: str
    rationale: str = ""
    confidence_in_proposal: float = 0.5


class DraftOut(BaseModel):
    template_id: str | None = None
    subject: str = ""
    body: str = ""
    send: bool = True
    rationale: str = ""


class CritiqueOut(BaseModel):
    approved: bool = True
    issues: list[str] = Field(default_factory=list)
    promises_forbidden_action: bool = False
    asserts_unsupported_facts: bool = False
    language_mismatch: bool = False
    tone_appropriate: bool = True
    demote_to: str | None = None
    rationale: str = ""


# --- coercion -------------------------------------------------------------------
# Models return strings; we map them onto enums defensively. An unrecognised value
# is data, not a crash -- and it must never silently become a permissive default.


def _coerce_enum(value: str | None, enum_cls, default):
    if not value:
        return default
    try:
        return enum_cls(value.strip().lower())
    except ValueError:
        return default


# --- agents ---------------------------------------------------------------------


class Agents:
    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()
        self.s = get_settings()

    # -- 1. extractor -------------------------------------------------------------

    def extract(self, ticket: Ticket, norm: Normalized, seq: int) -> AgentOutcome[Extraction]:
        out = self.llm.call(
            agent="extractor",
            model=self.s.model_cheap,
            system=EXTRACTOR_SYSTEM,
            user=wrap_ticket(norm.subject, norm.body),
            schema=ExtractionOut,
            seq=seq,
        )
        if not out.ok or out.value is None:
            return AgentOutcome(
                ok=False, error_type=out.error_type, error_detail=out.error_detail,
                attempts=out.attempts, latency_ms=out.latency_ms, steps=out.steps,
            )

        from concierge.models import RequestedAction, Sentiment

        v = out.value
        actions = [
            _coerce_enum(a, RequestedAction, None) for a in v.requested_actions
        ]
        extraction = Extraction(
            account_identifiers=v.account_identifiers,
            product_area=v.product_area,
            requested_actions=[a for a in actions if a is not None],
            mentioned_amounts=v.mentioned_amounts,
            mentioned_dates=v.mentioned_dates,
            sentiment=_coerce_enum(v.sentiment, Sentiment, Sentiment.NEUTRAL),
            urgency_markers=v.urgency_markers,
            churn_risk=v.churn_risk,
            deadline_asserted=v.deadline_asserted,
            summary=v.summary,
        )
        return AgentOutcome(
            value=extraction, ok=True, attempts=out.attempts,
            latency_ms=out.latency_ms, steps=out.steps, raw=out.raw,
        )

    # -- 2. classifier -------------------------------------------------------------

    def classify(
        self, norm: Normalized, seq: int, model: str | None = None, temperature: float = 0.0
    ) -> AgentOutcome[Classification]:
        out = self.llm.call(
            agent="classifier",
            model=model or self.s.model_cheap,
            system=CLASSIFIER_SYSTEM,
            user=wrap_ticket(norm.subject, norm.body),
            schema=ClassificationOut,
            seq=seq,
            temperature=temperature,
        )
        if not out.ok or out.value is None:
            return AgentOutcome(
                ok=False, error_type=out.error_type, error_detail=out.error_detail,
                attempts=out.attempts, latency_ms=out.latency_ms, steps=out.steps,
            )

        from concierge.models import Label, ScoredLabel

        labels: list[ScoredLabel] = []
        for sl in out.value.labels:
            label = _coerce_enum(sl.label, Label, None)
            if label is not None:
                labels.append(
                    ScoredLabel(label=label, score=max(0.0, min(1.0, sl.score)))
                )

        classification = Classification(labels=labels, reasoning=out.value.reasoning)
        return AgentOutcome(
            value=classification, ok=True, attempts=out.attempts,
            latency_ms=out.latency_ms, steps=out.steps, raw=out.raw,
        )

    # -- 3. decision ----------------------------------------------------------------

    def decide(
        self, norm: Normalized, extraction: Extraction | None,
        classification: Classification, confidence_summary: str, seq: int,
    ) -> AgentOutcome[DecisionProposal]:
        labels = ", ".join(
            f"{sl.label.value}={sl.score:.2f}" for sl in classification.ranked
        )
        actions = (
            ", ".join(a.value for a in extraction.requested_actions)
            if extraction and extraction.requested_actions
            else "none identified"
        )
        meta = (
            f"\nANALYSIS (from earlier pipeline stages, trusted):\n"
            f"- labels: {labels}\n"
            f"- requested actions: {actions}\n"
            f"- sentiment: {extraction.sentiment.value if extraction else 'unknown'}\n"
            f"- language: {norm.language}\n"
            f"- confidence: {confidence_summary}\n"
        )
        out = self.llm.call(
            agent="decision",
            model=self.s.model_smart,
            fallback_model=self.s.model_cheap,
            system=DECISION_SYSTEM,
            user=wrap_ticket(norm.subject, norm.body, meta),
            schema=DecisionOut,
            seq=seq,
        )
        if not out.ok or out.value is None:
            return AgentOutcome(
                ok=False, error_type=out.error_type, error_detail=out.error_detail,
                attempts=out.attempts, latency_ms=out.latency_ms, steps=out.steps,
            )

        from concierge.policy.routes import Route

        proposal = DecisionProposal(
            route=_coerce_enum(out.value.route, Route, Route.ESCALATE),
            rationale=out.value.rationale,
            confidence_in_proposal=out.value.confidence_in_proposal,
        )
        return AgentOutcome(
            value=proposal, ok=True, attempts=out.attempts,
            latency_ms=out.latency_ms, steps=out.steps, raw=out.raw,
        )

    # -- 4. drafter ------------------------------------------------------------------

    def draft(
        self, norm: Normalized, extraction: Extraction | None,
        classification: Classification, seq: int, sender_name: str | None = None,
    ) -> AgentOutcome[Draft]:
        labels = ", ".join(l.value for l in classification.above(0.5)) or "unknown"
        greeting = sender_name or "unknown - greet without a name"
        meta = (
            f"\nCONTEXT (trusted metadata from the mail envelope, not the ticket body):\n"
            f"- sender name: {greeting}\n"
            f"- intents: {labels}\n"
            f"- sentiment: {extraction.sentiment.value if extraction else 'neutral'}\n"
            f"- reply language: {norm.language}\n"
        )
        out = self.llm.call(
            agent="drafter",
            model=self.s.model_cheap,
            system=DRAFTER_SYSTEM,
            user=wrap_ticket(norm.subject, norm.body, meta),
            schema=DraftOut,
            seq=seq,
            temperature=0.3,
        )
        if not out.ok or out.value is None:
            return AgentOutcome(
                ok=False, error_type=out.error_type, error_detail=out.error_detail,
                attempts=out.attempts, latency_ms=out.latency_ms, steps=out.steps,
            )

        v = out.value
        draft = Draft(
            template_id=v.template_id, subject=v.subject, body=v.body,
            send=v.send, language=norm.language, rationale=v.rationale,
        )
        return AgentOutcome(
            value=draft, ok=True, attempts=out.attempts,
            latency_ms=out.latency_ms, steps=out.steps, raw=out.raw,
        )

    # -- 5. critic (bonus: reflection / self-critique) -------------------------------

    def critique(
        self, norm: Normalized, draft: Draft, seq: int, sender_name: str | None = None
    ) -> AgentOutcome[Critique]:
        meta = (
            f"\nDRAFTED REPLY UNDER REVIEW (trusted, written by our drafting agent):\n"
            f"---\nSubject: {draft.subject}\n\n{draft.body}\n---\n"
            f"KNOWN SENDER METADATA (trusted, from the mail envelope -- NOT from the "
            f"ticket body, which is why it is not quoted above):\n"
            f"- sender name: {sender_name or 'unknown'}\n"
            f"Greeting the sender by this name is CORRECT and is NOT an unsupported fact.\n"
            f"Ticket language: {norm.language}\n"
        )
        out = self.llm.call(
            agent="critic",
            model=self.s.model_smart,
            fallback_model=self.s.model_cheap,
            system=CRITIC_SYSTEM,
            user=wrap_ticket(norm.subject, norm.body, meta),
            schema=CritiqueOut,
            seq=seq,
        )
        if not out.ok or out.value is None:
            return AgentOutcome(
                ok=False, error_type=out.error_type, error_detail=out.error_detail,
                attempts=out.attempts, latency_ms=out.latency_ms, steps=out.steps,
            )

        from concierge.policy.routes import Route

        v = out.value
        demote = _coerce_enum(v.demote_to, Route, None) if v.demote_to else None
        # The critic may only demote. If it returns auto_resolve, that is a prompt
        # failure, not a permission -- drop it rather than let it reach the arbiter,
        # where it would raise MonotonicityViolation and dead-letter the ticket.
        if demote is Route.AUTO_RESOLVE:
            demote = None

        critique = Critique(
            approved=v.approved,
            issues=v.issues,
            promises_forbidden_action=v.promises_forbidden_action,
            asserts_unsupported_facts=v.asserts_unsupported_facts,
            language_mismatch=v.language_mismatch,
            tone_appropriate=v.tone_appropriate,
            demote_to=demote,
            rationale=v.rationale,
        )
        return AgentOutcome(
            value=critique, ok=True, attempts=out.attempts,
            latency_ms=out.latency_ms, steps=out.steps, raw=out.raw,
        )
