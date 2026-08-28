"""Routes and the severity algebra that carries the client's hard requirement.

The whole safety property of this system reduces to one idea:

    auto_resolve  <  draft_for_review  <  escalate

Every component in the pipeline may push a ticket TOWARD human review.
Nothing may pull it back toward automation.

That makes the safety guarantee auditable by reading this file, rather than by
trusting five LLM prompts to behave. See ADR-002.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from functools import total_ordering


@total_ordering
class Route(str, Enum):
    """A terminal disposition for a ticket, ordered by how much human attention it demands."""

    AUTO_RESOLVE = "auto_resolve"
    DRAFT_FOR_REVIEW = "draft_for_review"
    ESCALATE = "escalate"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Route):
            return NotImplemented
        return self.severity < other.severity


_SEVERITY: dict[Route, int] = {
    Route.AUTO_RESOLVE: 0,
    Route.DRAFT_FOR_REVIEW: 1,
    Route.ESCALATE: 2,
}


def most_conservative(*routes: Route | None) -> Route:
    """The single point at which a final route is decided.

    Returns the most severe of the supplied routes, ignoring ``None`` (a component
    that had no opinion). With no opinions at all, degrades to ESCALATE rather than
    to automation -- an empty set of judgements is not evidence that a ticket is safe.

    This function is the enforcement point for the monotonicity invariant. Any code
    path that assigns a final route MUST come through here.
    """
    opinions = [r for r in routes if r is not None]
    if not opinions:
        return Route.ESCALATE
    return max(opinions, key=lambda r: r.severity)


def is_demotion(before: Route, after: Route) -> bool:
    """True if `after` is more conservative than `before` (i.e. toward human review)."""
    return after.severity > before.severity


def assert_monotonic(before: Route, after: Route) -> None:
    """Guard against a component quietly promoting a ticket toward automation.

    Raised rather than logged: a promotion is a violation of the system's core
    safety property, and continuing past it would mean auto-sending something the
    policy layer had already refused.
    """
    if after.severity < before.severity:
        raise MonotonicityViolation(
            f"route was promoted {before.value} -> {after.value}; "
            "components may only demote toward human review"
        )


class MonotonicityViolation(RuntimeError):
    """A component tried to make a route less conservative. Always a bug."""


# --- Escalation is not one queue -------------------------------------------------
# TCK-1015 (security) and TCK-1010 (GDPR) must not land in the same place as a
# frustrated billing customer. The trail records which queue was chosen.


class EscalationQueue(str, Enum):
    SUPPORT = "support"        # general human agent
    BILLING = "billing"        # money movement, disputes
    SECURITY = "security"      # vulnerability reports, suspected attacks
    LEGAL = "legal"            # statutory requests, legal threats
    RETENTION = "retention"    # churn risk, commercial concessions


class Priority(str, Enum):
    P0 = "P0"  # security incident, active data exposure
    P1 = "P1"  # angry customer, SLA-bound, churn risk
    P2 = "P2"  # normal
    P3 = "P3"  # informational

    @property
    def rank(self) -> int:
        return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}[self.value]


def most_urgent(*priorities: Priority | None) -> Priority:
    """Most urgent (lowest rank) of the supplied priorities; P2 if none given."""
    given = [p for p in priorities if p is not None]
    if not given:
        return Priority.P2
    return min(given, key=lambda p: p.rank)


def all_routes() -> Iterable[Route]:
    return tuple(Route)
