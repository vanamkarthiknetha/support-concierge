"""Deterministic text normalization. No LLM.

Rationale (ADR-006): TCK-1017 arrives as quoted-printable mojibake with leetspeak
obfuscation. `quopri.decodestring()` solves most of it for free. Spending model
tokens on a problem the standard library already solves is both wasteful and less
reliable -- and it lets a mail-transport artefact masquerade as an ambiguous customer.

Everything here is unit-testable with no API key and no cost.
"""

from __future__ import annotations

import hashlib
import quopri
import re
import unicodedata

# Subject prefix chains: "Re: Re: FWD: issue" -> "issue"  (TCK-1017)
_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|tr)\s*:\s*)+", re.IGNORECASE)

# Quoted-printable soft line breaks and =XX escapes
_QP_MARKER = re.compile(r"=[0-9A-Fa-f]{2}|=\r?\n")

# Runs of the Unicode replacement character, plus the literal "????" that mail
# gateways leave behind when they transcode badly.
_REPLACEMENT_RUN = re.compile(r"[�﻿]+")
_QUESTION_RUN = re.compile(r"\?{3,}")

_WHITESPACE = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# Leetspeak: only applied inside otherwise-alphabetic words, so we don't damage
# real tokens like "account_id", "P0", "$12", or "90 days".
_LEET_MAP = str.maketrans(
    {"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s"}
)
_LEET_CANDIDATE = re.compile(
    r"\b(?=[a-z]*[0-9@$])(?=[0-9@$]*[a-z])[a-z0-9@$]{3,}\b", re.IGNORECASE
)

# Words we must never "de-leet" -- they're legitimately alphanumeric.
_LEET_SKIP = frozenset(
    {"p0", "p1", "p2", "p3", "h1", "h2", "b2b", "s3", "ec2", "oauth2", "utf8", "sha256", "md5"}
)


def decode_quoted_printable(text: str) -> tuple[str, bool]:
    """Decode QP if the text looks QP-encoded. Returns (text, did_decode)."""
    if not _QP_MARKER.search(text):
        return text, False
    try:
        decoded = quopri.decodestring(text.encode("utf-8", "surrogateescape"))
        return decoded.decode("utf-8", "replace"), True
    except Exception:
        return text, False


def strip_mojibake(text: str) -> tuple[str, bool]:
    """Remove replacement-character runs and long '????' sequences."""
    out, n1 = _REPLACEMENT_RUN.subn(" ", text)
    out, n2 = _QUESTION_RUN.subn(" ", out)
    return out, bool(n1 or n2)


def deleet(text: str) -> tuple[str, bool]:
    """Normalize leetspeak inside mixed alphanumeric words: 'sh0wing' -> 'showing'."""
    changed = False

    def repl(m: re.Match[str]) -> str:
        nonlocal changed
        word = m.group(0)
        if word.lower() in _LEET_SKIP:
            return word
        # Don't touch words that are mostly digits (version numbers, ids).
        digits = sum(c.isdigit() for c in word)
        if digits > len(word) / 2:
            return word
        fixed = word.translate(_LEET_MAP)
        if fixed != word:
            changed = True
        return fixed

    return _LEET_CANDIDATE.sub(repl, text), changed


def clean_subject(subject: str) -> tuple[str, bool]:
    """Strip Re:/Fwd: chains."""
    cleaned = _SUBJECT_PREFIX.sub("", subject).strip()
    return (cleaned or subject.strip()), cleaned != subject.strip()


def collapse_whitespace(text: str) -> str:
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def estimate_tokens(text: str) -> int:
    """Rough token count. Good enough for a signal-poverty threshold."""
    return len(text.split())


def text_quality(original: str, cleaned: str, repairs: list[str]) -> float:
    """How much did we have to repair? 1.0 = pristine, lower = more damage.

    Feeds the `text_corruption` confidence penalty. A ticket we had to heavily
    repair is one we should be less confident we understood.
    """
    if not original.strip():
        return 0.0

    score = 1.0
    if "quoted_printable" in repairs:
        score -= 0.25
    if "mojibake" in repairs:
        score -= 0.25
    if "leetspeak" in repairs:
        score -= 0.15
    if "subject_chain" in repairs:
        score -= 0.05

    # Non-ASCII garbage ratio in the ORIGINAL is independent evidence of damage.
    printable = sum(1 for c in original if c.isprintable() or c.isspace())
    if printable / max(len(original), 1) < 0.9:
        score -= 0.2

    return max(0.0, min(1.0, score))


def dedup_hash(subject: str, body: str, from_email: str | None) -> str:
    """Stable hash over normalized content + sender, for follow-up detection."""
    basis = f"{(from_email or '').lower()}|{subject.lower()}|{body.lower()}"
    basis = re.sub(r"\W+", " ", basis).strip()
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def normalize_text(subject: str, body: str) -> tuple[str, str, list[str], float]:
    """Full normalization pass.

    Returns (subject, body, repairs, text_quality).
    """
    original = f"{subject}\n{body}"
    repairs: list[str] = []

    body_out, did_qp = decode_quoted_printable(body)
    if did_qp:
        repairs.append("quoted_printable")

    body_out, did_moji = strip_mojibake(body_out)
    if did_moji:
        repairs.append("mojibake")

    body_out, did_leet = deleet(body_out)
    if did_leet:
        repairs.append("leetspeak")

    subject_out, did_chain = clean_subject(subject)
    if did_chain:
        repairs.append("subject_chain")

    subject_out = unicodedata.normalize("NFKC", collapse_whitespace(subject_out))
    body_out = unicodedata.normalize("NFKC", collapse_whitespace(body_out))

    return subject_out, body_out, repairs, text_quality(original, body_out, repairs)
