"""The policy gate: deterministic route ceiling. ADR-002.

    "Nothing involving money (refunds, billing changes, cancellations), account
     deletion, legal threats, or security reports may be auto-resolved without a
     human in the loop -- regardless of how confident the system is."

That requirement is absolute, so it cannot live in a prompt. A rule expressed in a
prompt is a rule the model can be argued out of, and TCK-1013 is a live example of
someone trying. This module is plain Python: no model output reaches it except as
data, and no confidence value can clear a hard block.

Every rule that fires is recorded -- not just the first match. TCK-1010 fires both
`legal_request` and `account_deletion`; if one rule is later found miscalibrated,
the trail still shows the ticket was independently covered.
"""

from __future__ import annotations

from concierge.models import (
    Classification,
    Extraction,
    Label,
    Normalized,
    PolicyResult,
    PolicyRuleFire,
    RequestedAction,
    Sentiment,
)
from concierge.policy.routes import (
    EscalationQueue,
    Priority,
    Route,
    most_conservative,
    most_urgent,
)

RULES_VERSION = "1"

# Categories the client named. No confidence value unlocks these.
HARD_BLOCK_LABELS: dict[Label, tuple[EscalationQueue, Priority]] = {
    Label.REFUND_REQUEST: (EscalationQueue.BILLING, Priority.P2),
    Label.BILLING_DISPUTE: (EscalationQueue.BILLING, Priority.P1),
    Label.SUBSCRIPTION_CHANGE: (EscalationQueue.BILLING, Priority.P2),
    Label.ACCOUNT_DELETION: (EscalationQueue.SUPPORT, Priority.P1),
    Label.SECURITY_REPORT: (EscalationQueue.SECURITY, Priority.P0),
    Label.LEGAL_REQUEST: (EscalationQueue.LEGAL, Priority.P1),
}

LABEL_THRESHOLD = 0.5

# Languages we hold verified response templates for. Anything else caps at
# draft_for_review -- we never auto-send a machine-translated reply as if a human
# wrote it (TCK-1007).
SUPPORTED_LANGUAGES = frozenset({"en"})


class PolicyGate:
    """Computes the route ceiling. Pure function of its inputs."""

    def evaluate(
        self,
        classification: Classification | None,
        extraction: Extraction | None,
        normalized: Normalized | None,
    ) -> PolicyResult:
        fires: list[PolicyRuleFire] = []
        ceiling = Route.AUTO_RESOLVE
        queues: list[EscalationQueue] = []
        priorities: list[Priority] = []
        hard_blocked = False

        def fire(
            rule_id: str,
            triggered_by: str,
            to: Route,
            detail: str = "",
            queue: EscalationQueue | None = None,
            priority: Priority | None = None,
        ) -> None:
            nonlocal ceiling
            before = ceiling
            after = most_conservative(before, to)
            fires.append(
                PolicyRuleFire(
                    rule_id=rule_id,
                    rule_version=RULES_VERSION,
                    triggered_by=triggered_by,
                    route_before=before,
                    route_after=after,
                    detail=detail,
                )
            )
            ceiling = after
            if queue:
                queues.append(queue)
            if priority:
                priorities.append(priority)

        # --- 1. missing classification ------------------------------------------
        # Without labels the hard blocks cannot be evaluated at all. Proceeding
        # here is precisely the unsafe-automation case, so we fail closed.
        if classification is None or not classification.labels:
            fire(
                "no_classification",
                "classifier produced no labels",
                Route.ESCALATE,
                "cannot evaluate hard blocks without labels; failing closed",
                EscalationQueue.SUPPORT,
            )
            return PolicyResult(
                ceiling=ceiling,
                fires=fires,
                queue=EscalationQueue.SUPPORT,
                priority=Priority.P2,
                hard_blocked=True,
            )

        active = classification.above(LABEL_THRESHOLD)

        # --- 2. injection ---------------------------------------------------------
        # A floor, not a penalty: nothing downstream can outweigh it.
        if normalized and normalized.injection_suspected:
            patterns = ", ".join(sorted({s.pattern for s in normalized.injection_spans}))
            fire(
                "injection_suspected",
                f"detectors: {patterns}",
                Route.ESCALATE,
                "ticket contains text resembling instructions aimed at the system; "
                "content quarantined and never re-prompted",
                EscalationQueue.SECURITY,
                Priority.P1,
            )
            hard_blocked = True

        # --- 3. hard-block labels -------------------------------------------------
        # Risk is MAX over labels, never mean: one blocked label contaminates the
        # whole ticket (TCK-1006, where a bug report carries a double-charge).
        for label in active:
            if label in HARD_BLOCK_LABELS:
                queue, prio = HARD_BLOCK_LABELS[label]
                score = next(
                    (sl.score for sl in classification.labels if sl.label == label), 0.0
                )
                fire(
                    f"hard_block.{label.value}",
                    f"label={label.value} score={score:.2f}",
                    Route.ESCALATE,
                    "client policy: category requires a human regardless of confidence",
                    queue,
                    prio,
                )
                hard_blocked = True

        # --- 4. requested action moves money --------------------------------------
        # The distinction the whole design turns on: is a monetary STATE CHANGE
        # being requested? TCK-1001 asks for an explanation (no) and drafts;
        # TCK-1018 asks for "flexibility" -- a discount -- (yes) and escalates.
        if extraction and extraction.requests_money_movement:
            actions = [a.value for a in extraction.requested_actions if a.moves_money]
            fire(
                "money_movement_requested",
                f"requested_actions={actions}",
                Route.ESCALATE,
                "granting this request would change the customer's monetary state",
                EscalationQueue.BILLING,
                Priority.P1 if extraction.churn_risk else Priority.P2,
            )
            hard_blocked = True

        if extraction and RequestedAction.DATA_DELETION in extraction.requested_actions:
            fire(
                "data_deletion_requested",
                "requested_actions=[data_deletion]",
                Route.ESCALATE,
                "irreversible data action requires a human",
                EscalationQueue.LEGAL,
                Priority.P1,
            )
            hard_blocked = True

        # --- 5. non-blocking caps -------------------------------------------------
        # These raise the floor to draft_for_review without being hard blocks.

        if normalized and normalized.language not in SUPPORTED_LANGUAGES:
            fire(
                "unsupported_language",
                f"language={normalized.language}",
                Route.DRAFT_FOR_REVIEW,
                "no verified response template in this language; a machine-translated "
                "auto-reply would be sent as if a human wrote it",
            )

        if normalized and normalized.is_followup:
            fire(
                "repeat_contact",
                f"related={normalized.related_tickets}",
                Route.DRAFT_FOR_REVIEW,
                "customer is following up on an unresolved issue; re-sending the same "
                "canned acknowledgement is worse than sending nothing",
                priority=Priority.P1,
            )

        if len(active) >= 2 and not hard_blocked:
            fire(
                "multi_intent",
                f"labels={[l.value for l in active]}",
                Route.DRAFT_FOR_REVIEW,
                "multiple distinct intents; a single canned response cannot address both",
            )

        if extraction and extraction.sentiment == Sentiment.ANGRY:
            fire(
                "angry_sentiment",
                "sentiment=angry",
                Route.DRAFT_FOR_REVIEW,
                "hostile tone; an automated reply risks escalating the situation",
                priority=Priority.P1,
            )

        if extraction and extraction.churn_risk:
            fire(
                "churn_risk",
                "churn_risk=true",
                Route.DRAFT_FOR_REVIEW,
                "explicit churn signal; retention judgement needed",
                EscalationQueue.RETENTION,
                Priority.P1,
            )

        if extraction and extraction.deadline_asserted:
            fire(
                "asserted_deadline",
                f"deadline={extraction.deadline_asserted}",
                Route.DRAFT_FOR_REVIEW,
                "sender asserts a deadline; may carry an SLA or statutory obligation",
                priority=Priority.P1,
            )

        # --- resolve queue / priority ---------------------------------------------
        queue = _pick_queue(queues)
        priority = most_urgent(*priorities) if priorities else Priority.P2

        if Label.SPAM in active and not hard_blocked:
            priority = Priority.P3
        if Label.POSITIVE_FEEDBACK in active and not hard_blocked:
            priority = Priority.P3

        return PolicyResult(
            ceiling=ceiling,
            fires=fires,
            queue=queue if ceiling == Route.ESCALATE else None,
            priority=priority,
            hard_blocked=hard_blocked,
        )


# Escalation is not one bucket. When several queues are implicated, the most
# specialised wins -- a cross-tenant data leak must not land in general support
# just because the ticket also mentions billing.
_QUEUE_PRECEDENCE = (
    EscalationQueue.SECURITY,
    EscalationQueue.LEGAL,
    EscalationQueue.RETENTION,
    EscalationQueue.BILLING,
    EscalationQueue.SUPPORT,
)


def _pick_queue(queues: list[EscalationQueue]) -> EscalationQueue | None:
    if not queues:
        return None
    for q in _QUEUE_PRECEDENCE:
        if q in queues:
            return q
    return queues[0]
