"""The normalization node: deterministic pre-processing. No LLM, no cost, no failure mode.

Owns TCK-1017 (corrupted encoding), TCK-1014 (duplicate detection), TCK-1007
(language), and layer 2 of the TCK-1013 injection defence.
"""

from __future__ import annotations

import re
from typing import Protocol

from concierge.models import Normalized, Ticket
from concierge.normalize import injection
from concierge.normalize.text import dedup_hash, estimate_tokens, normalize_text


class TicketLookup(Protocol):
    """Read side of the store, used for follow-up detection."""

    def find_related(self, from_email: str | None, dedup_hash: str) -> list[str]: ...


# Language detection: langdetect is unreliable on very short text, so we guard it
# with a length check and fall back to 'en'. Getting this wrong in the confident
# direction would cap a legitimate English ticket at draft_for_review.
_MIN_CHARS_FOR_LANGDETECT = 25


def detect_language(text: str) -> str:
    if len(text.strip()) < _MIN_CHARS_FOR_LANGDETECT:
        return "en"
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0  # deterministic; audit trails must be reproducible
        return detect(text)
    except Exception:
        return "en"


# Follow-up phrasing is strong, cheap evidence -- stronger than text similarity
# alone, because TCK-1014 rewords the complaint rather than repeating it.
_FOLLOWUP_PHRASES = re.compile(
    r"\b(again|still\s+(isn'?t|not|doesn'?t)|follow(ing)?\s*up|"
    r"reported\s+this|as\s+(i|we)\s+(said|mentioned)|"
    r"second\s+time|third\s+time|any\s+update|chasing)\b",
    re.IGNORECASE,
)


def normalize(ticket: Ticket, lookup: TicketLookup | None = None) -> Normalized:
    subject, body, repairs, quality = normalize_text(ticket.subject, ticket.body)

    # Injection scan runs on the REPAIRED text: an attacker could otherwise hide a
    # payload behind quoted-printable encoding and slip past the detectors.
    combined = f"{subject}\n{body}"
    suspected, spans, _score = injection.scan(combined)

    if suspected:
        body = injection.quarantine(body, _spans_within(spans, subject, body))
        repairs.append("injection_quarantined")

    language = detect_language(body)
    dhash = dedup_hash(subject, body, ticket.from_email)

    related: list[str] = []
    if lookup is not None:
        related = [t for t in lookup.find_related(ticket.from_email, dhash) if t != ticket.id]

    is_followup = bool(related) or bool(_FOLLOWUP_PHRASES.search(combined))

    return Normalized(
        subject=subject,
        body=body,
        language=language,
        text_quality=quality,
        repairs=repairs,
        token_estimate=estimate_tokens(f"{subject} {body}"),
        injection_suspected=suspected,
        injection_spans=spans,
        dedup_hash=dhash,
        related_tickets=related,
        is_followup=is_followup,
    )


def _spans_within(spans, subject: str, body: str):
    """Re-base span offsets from the combined string onto the body alone."""
    offset = len(subject) + 1
    out = []
    for s in spans:
        if s.start >= offset:
            out.append(s.model_copy(update={"start": s.start - offset, "end": s.end - offset}))
    return out
