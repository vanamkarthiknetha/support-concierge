# Supporting documents

The main [README](../README.md) is the submission. These are the working documents behind it —
included because the brief grades *reasoning*, and the reasoning is more legible in its
original form than summarised.

| Document | What it is | Why it's here |
|---|---|---|
| [DECISIONS.md](DECISIONS.md) | 13 ADRs, each with its **trade-off named** | The source the README's "key decisions" section is written from. A decision without a stated cost reads as a preference. |
| [PHASE0-FINDINGS.md](PHASE0-FINDINGS.md) | What 30 minutes of probing the live API changed about the design | Three of the plan's assumptions were wrong — logprobs unavailable, self-consistency measured flat, label margin discriminates. The probe scripts in [`../backend/scripts/`](../backend/scripts/) reproduce every number. |
| [TICKET-ANALYSIS.md](TICKET-ANALYSIS.md) | All 18 sample tickets, one named mechanism each | The raw material for README section 3 and for the gold labels in [`../backend/evals/gold.jsonl`](../backend/evals/gold.jsonl). Fuller than the README summary. |

## Reading order

If you have 5 minutes: the main README's *Results* and *The idea in one line* sections, then
[`policy/arbiter.py`](../backend/concierge/policy/arbiter.py) — ~40 lines that carry the whole
safety guarantee.

If you have 20: add [DECISIONS.md](DECISIONS.md) ADR-002 (safety in code, not prompts) and
ADR-003 (composite confidence), then [PHASE0-FINDINGS.md](PHASE0-FINDINGS.md) for what
measurement changed.

If you want the adversarial case: [TICKET-ANALYSIS.md](TICKET-ANALYSIS.md) → TCK-1013, then run
`uv run concierge show TCK-1013` to see the four defence layers fire in order.
