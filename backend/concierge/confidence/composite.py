"""The confidence signal. ADR-003, revised by measurement (phase0-findings.md).

The brief explicitly rejects "a fixed rule keyed only on category" and asks where
the signal comes from. Here is the honest answer, including what we tried and
discarded:

  c_margin      top1 - top2 of the ranked labels.  PRIMARY. Free (already in the
                response). Measured 0.96 on a clean bug report, 0.05 on a
                genuinely multi-intent ticket -- it ranks the sample correctly.

  c_crossmodel  Jaccard agreement between the cheap and smart models. ADAPTIVE:
                only runs when c_margin lands in the uncertainty band, because
                free-tier quota is ~15 RPM and this doubles classifier calls.

  c_selfreport  The model's own top-1 score. Weak and poorly calibrated; clamped
                and given the lowest weight. Kept because disagreement between
                it and the margin is itself informative.

  penalties     Deterministic heuristics. These do the heavy lifting -- and they
                are the only signals that can detect MISSING evidence, which no
                model-derived score can (see TCK-1009 below).

  REMOVED: c_consistency (k-sample agreement). Measured at k=4/temp=1.0 on a
  deliberately ambiguous ticket: 1 distinct label-set, c_consistency = 1.00.
  Schema-constrained decoding over a fixed taxonomy collapses sampling variance,
  so the signal never varied. Shipping it would have made the design look
  rigorous while doing nothing.

  REMOVED: c_logprob. `response_logprobs` is rejected by every Gemini Flash model
  on the AI Studio API (400: "Logprobs is not enabled for this model"). The
  Google guidance describing avg_logprobs is Vertex AI, a different surface.
"""

from __future__ import annotations

from concierge.models import (
    Classification,
    ConfidenceBreakdown,
    Extraction,
    Normalized,
)

# Weights sum to 1.0 and are applied as EXPONENTS in a geometric mean.
W_MARGIN = 0.5
W_CROSSMODEL = 0.3
W_SELFREPORT = 0.2

SELFREPORT_FLOOR = 0.3
SELFREPORT_CEIL = 0.95

# Below this many tokens a ticket carries too little evidence to act on (TCK-1009).
SIGNAL_POVERTY_TOKENS = 15

PENALTY_WEIGHTS = {
    "signal_poverty": 0.45,
    "text_corruption": 0.30,
    "language_mismatch": 0.35,
    "multi_intent": 0.25,
    "is_followup": 0.20,
    "no_extraction": 0.20,
}

MAX_TOTAL_PENALTY = 0.9


def compute_penalties(
    normalized: Normalized | None,
    classification: Classification | None,
    extraction: Extraction | None,
) -> dict[str, float]:
    """Deterministic confidence penalties. No model involved.

    These are the only signals capable of noticing that evidence is ABSENT.
    A model asked to classify "help" will confidently answer `bug_report`
    (measured: margin 0.70) -- it cannot report that it had nothing to go on.
    """
    p: dict[str, float] = {}

    if normalized is not None:
        if normalized.token_estimate < SIGNAL_POVERTY_TOKENS:
            # Scale by how far below the threshold: "help" is worse than a short
            # but complete sentence.
            severity = 1.0 - (normalized.token_estimate / SIGNAL_POVERTY_TOKENS)
            p["signal_poverty"] = round(PENALTY_WEIGHTS["signal_poverty"] * severity, 3)

        if normalized.text_quality < 0.8:
            severity = (0.8 - normalized.text_quality) / 0.8
            p["text_corruption"] = round(PENALTY_WEIGHTS["text_corruption"] * severity, 3)

        if normalized.language != "en":
            p["language_mismatch"] = PENALTY_WEIGHTS["language_mismatch"]

        if normalized.is_followup:
            p["is_followup"] = PENALTY_WEIGHTS["is_followup"]

    if classification is not None and len(classification.above(0.5)) >= 2:
        p["multi_intent"] = PENALTY_WEIGHTS["multi_intent"]

    if extraction is None:
        p["no_extraction"] = PENALTY_WEIGHTS["no_extraction"]

    return p


def compute(
    classification: Classification | None,
    extraction: Extraction | None,
    normalized: Normalized | None,
    c_crossmodel: float | None = None,
) -> ConfidenceBreakdown:
    """Combine every signal into one composite score in [0, 1]."""
    notes: list[str] = []

    if classification is None or not classification.labels:
        return ConfidenceBreakdown(
            composite=0.0,
            notes=["no classification available; confidence is zero by construction"],
        )

    c_margin = classification.margin
    top = classification.top
    c_selfreport = min(max(top.score if top else 0.0, SELFREPORT_FLOOR), SELFREPORT_CEIL)

    penalties = compute_penalties(normalized, classification, extraction)

    # Injection is a FLOOR, not a penalty term: nothing may outweigh it.
    if normalized is not None and normalized.injection_suspected:
        return ConfidenceBreakdown(
            c_margin=round(c_margin, 4),
            c_crossmodel=c_crossmodel,
            c_selfreport=round(c_selfreport, 4),
            penalties=penalties,
            composite=0.0,
            notes=[
                "injection_suspected: automation confidence clamped to 0. "
                "This is a floor, not a weighted term."
            ],
        )

    # Geometric mean, not arithmetic. With an arithmetic mean three confident
    # signals average away one near-zero signal; geometrically, any component near
    # zero drags the product down. For a system whose stated bias is caution, a
    # single strong disagreement SHOULD dominate.
    if c_crossmodel is None:
        # Renormalize the remaining weights so skipping the adaptive check does not
        # silently deflate the score.
        total = W_MARGIN + W_SELFREPORT
        c_model = (c_margin ** (W_MARGIN / total)) * (c_selfreport ** (W_SELFREPORT / total))
        notes.append("cross-model check skipped (margin outside the uncertainty band)")
    else:
        c_model = (
            (c_margin**W_MARGIN)
            * (c_crossmodel**W_CROSSMODEL)
            * (c_selfreport**W_SELFREPORT)
        )

    penalty_total = min(sum(penalties.values()), MAX_TOTAL_PENALTY)
    composite = c_model * (1.0 - penalty_total)

    if penalties:
        dominant = max(penalties.items(), key=lambda kv: kv[1])
        notes.append(f"dominant penalty: {dominant[0]} (-{dominant[1]:.2f})")

    return ConfidenceBreakdown(
        c_margin=round(c_margin, 4),
        c_crossmodel=round(c_crossmodel, 4) if c_crossmodel is not None else None,
        c_selfreport=round(c_selfreport, 4),
        penalties=penalties,
        composite=round(max(0.0, min(1.0, composite)), 4),
        notes=notes,
    )


def jaccard(a: set[str], b: set[str]) -> float:
    """Cross-model agreement. Jaccard rather than exact match, so a partial
    overlap on a multi-label ticket isn't scored the same as total disagreement."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def needs_crossmodel_check(c_margin: float, low: float, high: float) -> bool:
    """Is this ticket close enough to a threshold that a second opinion is worth
    the quota? Confident and hopeless cases both skip it."""
    return low <= c_margin <= high
