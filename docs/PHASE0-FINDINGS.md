# Phase 0 Findings — measured, not assumed

Run 2026-08-28 against the live Gemini AI Studio API. These override the pre-build assumptions
in the main README and the Confidence section of the main README.

Scripts: [`backend/scripts/`](../backend/scripts/) — kept in
the repo so the findings are reproducible rather than asserted.

---

## 1. Logprobs are unavailable on every Gemini Flash model

```
gemini-3.7-flash       no - rejects response_logprobs
gemini-3.6-flash       no - rejects response_logprobs
gemini-3.5-flash       no - rejects response_logprobs
gemini-3.5-flash-lite  no - rejects response_logprobs
gemini-3.1-flash-lite  no - rejects response_logprobs
gemini-2.5-flash       no - rejects response_logprobs
gemini-2.5-flash-lite  no - rejects response_logprobs
```

`400 INVALID_ARGUMENT: Logprobs is not enabled for this model` on all seven.

**Why the plan assumed otherwise:** the Google source describing `avg_logprobs` is about
**Vertex AI**, a different surface requiring a GCP project and ADC auth. It does not apply to
the AI Studio API. Corrected.

**Impact:** `c_logprob` is deleted from the confidence composite. This was the signal that made
the composite "not just the model's self-report", so it needed a real replacement, not removal.

---

## 2. Self-consistency does not discriminate — it is flat at 1.00

The plan's *primary* signal. Measured on TCK-1006, a deliberately multi-intent ticket:

| temp | k | distinct label-sets | `c_consistency` |
|---|---|---|---|
| 0.7 | 3 | 1 | 1.00 |
| 1.0 | 4 | 1 | 1.00 |

Every sample returned exactly `['billing_dispute', 'billing_question', 'bug_report']`. Same
result on all four probe tickets, including TCK-1009 ("help").

**Why:** schema-constrained decoding over a fixed 13-label taxonomy collapses sampling variance.
The model isn't choosing freely between phrasings — it's filling a small enum. Temperature has
almost nothing to act on.

**Decision: drop `c_consistency`.** It costs k× the calls and returns 1.00 regardless of how
ambiguous the ticket is. A signal that never varies is decoration, and shipping it would have
made the confidence design look rigorous while doing nothing.

**This is a README beat, not an embarrassment.** "We implemented self-consistency, measured it
at k=4/temp=1.0, found it constant across deliberately ambiguous tickets, and removed it" is a
stronger answer to *"where does your confidence signal come from"* than shipping it unmeasured.
Keep the probe script as evidence.

---

## 3. Label margin DOES discriminate — it becomes the primary signal

`c_margin` = (top-1 score) − (top-2 score) from a ranked multi-label classification. Free: no
extra call, it's already in the response.

| ticket | margin | labels returned | reading |
|---|---|---|---|
| TCK-1002 clean bug | **0.96** | `bug_report` | unambiguous, safe to automate |
| TCK-1009 "help" | 0.70 | `bug_report` | model guesses; **penalty layer must catch this** |
| TCK-1005 billing rage | **0.13** | `billing_dispute`, `billing_question` | genuinely contested |
| TCK-1006 two issues | **0.05** | `billing_dispute`, `billing_question`, `bug_report` | correctly reads as multi-intent |

It ranks the tickets in the right order, and it is near-zero on exactly the two tickets we
independently know are hard. This is a better signal than logprobs would have been for this
task — logprobs measure token-level fluency, margin measures *decision* ambiguity.

**Note on TCK-1009:** margin 0.70 is misleadingly high — the model confidently guesses
`bug_report` from two words. Confirms the design was right to put the `signal_poverty`
**deterministic penalty** on the critical path. Model-derived signals cannot detect their own
missing evidence.

---

## 4. `gemini-3.7-flash` is unavailable; `gemini-3.5-flash` works

```
gemini-3.7-flash   ['503 UNAVAILABLE', '503 UNAVAILABLE']   (consistent, not transient)
gemini-3.5-flash   ['ok', 'ok']
gemini-2.5-flash   ['ok', 'ok']
```

**Decision:** `MODEL_SMART = gemini-3.5-flash`. The two-tier design (ADR-008) survives intact —
only the identity of the smart tier changes. Reinforces the "pin exact model IDs" rule: a
floating alias would have failed at runtime instead of at setup.

---

## 5. Free-tier rate limit ≈ 15 requests/minute

`429 RESOURCE_EXHAUSTED` after 13 calls in 21s.

The pipeline needs ~4 LLM calls/ticket × 18 tickets = **~72 calls** for one full run, plus
chaos runs and the threshold sweep.

**Decision: a token-bucket rate limiter is mandatory infrastructure, not a nice-to-have.**
Global limiter (~12 RPM, under the limit), 429 → respect `retry-after`, exponential backoff,
and treat sustained 429 as a circuit-breaker trip → escalate everything. A full run takes
~6 minutes wall-clock; acceptable, and it exercises the throttling path for real.

Also caps adaptive behaviour: cross-model checks only fire in the uncertainty band, because
quota is now a design constraint rather than a cost abstraction.

---

## Revised confidence composite

```python
# was: c_consistency^0.5 * c_logprob^0.3 * c_selfreport^0.2
c_model = (c_margin ** 0.5) * (c_crossmodel ** 0.3) * (c_selfreport ** 0.2)
composite = c_model * (1 - min(sum(penalties), 0.9))
if injection_suspected:
    composite = 0.0
```

| signal | source | status |
|---|---|---|
| `c_margin` | top1 − top2 of ranked labels | **primary**, validated, free |
| `c_crossmodel` | Jaccard(cheap labels, smart labels) | **adaptive** — only in the uncertainty band (quota) |
| `c_selfreport` | top-1 score, clamped `[0.3, 0.95]` | weak, lowest weight |
| penalties | deterministic heuristics | unchanged — does the heavy lifting |
| ~~`c_consistency`~~ | ~~k-sample agreement~~ | **removed** — measured flat |
| ~~`c_logprob`~~ | ~~`avg_logprobs`~~ | **removed** — unavailable on this API surface |

Still four independent sources, still not a fixed category rule, and now every one of them is
backed by a measurement rather than an assumption.
