"""Canned responses for the auto-resolve path.

Templates rather than generated text where a fixed answer is correct: it is
cheaper, it cannot hallucinate, and it is reviewable once instead of every time.

`clarification_request` is the important one -- it is what makes ADR-005 work.
Low confidence caused by MISSING INFORMATION on a low-risk ticket should produce a
question, not an escalation. Sending "help" to a human is exactly the low-value
work this system exists to remove.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    id: str
    subject: str
    body: str
    send: bool = True


TEMPLATES: dict[str, Template] = {
    "clarification_request": Template(
        id="clarification_request",
        subject="Re: {subject}",
        body=(
            "Hi{name},\n\n"
            "Thanks for getting in touch. I want to help, but I need a little more to "
            "go on.\n\n"
            "Could you tell us:\n"
            "  - what you were trying to do when it went wrong\n"
            "  - what you saw instead (an error message, a blank screen, nothing at all)\n"
            "  - which page or feature you were on\n\n"
            "Reply to this email and we'll pick it straight up.\n\n"
            "The Support Team"
        ),
    ),
    "bug_acknowledgement": Template(
        id="bug_acknowledgement",
        subject="Re: {subject}",
        body=(
            "Hi{name},\n\n"
            "Thanks for reporting this, and sorry for the trouble. We've logged the "
            "issue with the engineering team along with the details you gave us.\n\n"
            "We'll email you as soon as there's a fix. If you find a workaround or the "
            "behaviour changes, replying here adds to the same report.\n\n"
            "The Support Team"
        ),
    ),
    "feature_request_logged": Template(
        id="feature_request_logged",
        subject="Re: {subject}",
        body=(
            "Hi{name},\n\n"
            "Thanks for the suggestion. We've passed it to the product team and added "
            "it to the list they review when planning.\n\n"
            "We can't promise a date, but requests from customers genuinely do shape "
            "what gets built.\n\n"
            "The Support Team"
        ),
    ),
    "password_reset_help": Template(
        id="password_reset_help",
        subject="Re: {subject}",
        body=(
            "Hi{name},\n\n"
            "Sorry you're locked out. A few things that usually explain a missing "
            "reset email:\n\n"
            "  - it can take up to 10 minutes to arrive\n"
            "  - check spam, and any filters that route mail out of your inbox\n"
            "  - the reset only sends to the exact address on the account\n\n"
            "If none of that works, reply here and we'll sort it out from our side.\n\n"
            "The Support Team"
        ),
    ),
    "thanks_acknowledgement": Template(
        id="thanks_acknowledgement",
        subject="Re: {subject}",
        body=(
            "Hi{name},\n\n"
            "Thank you - that's genuinely good to hear, and we've passed it on to the "
            "person who helped you.\n\n"
            "The Support Team"
        ),
    ),
    # Replying to a spammer is a bug, not a courtesy. Close silently.
    "spam_close": Template(
        id="spam_close",
        subject="",
        body="",
        send=False,
    ),
}


def render(template_id: str, subject: str, from_name: str | None) -> tuple[str, str, bool]:
    """Render a template. Returns (subject, body, send)."""
    t = TEMPLATES[template_id]
    name = f" {from_name.split()[0]}" if from_name and from_name.strip() else ""
    # Don't greet a placeholder name by name.
    if from_name and from_name.strip().lower() in {"unknown", "unknown user", "anonymous"}:
        name = ""
    return (
        t.subject.format(subject=subject),
        t.body.format(name=name),
        t.send,
    )
