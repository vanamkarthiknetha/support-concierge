# Decision Log (ADRs)

Append-only. The README's "key decisions and what you traded off" section is written from this
file. Every entry states the **trade-off**, not just the choice — the brief grades reasoning,
and a decision without a named cost reads as a preference.

---

### ADR-001 — Orchestration: LangGraph state graph with conditional edges

**Decision.** A `StateGraph` whose nodes are agents and whose edges are deterministic Python
predicates.

**Alternatives rejected:**
- *Sequential pipeline.* Too rigid. Can't short-circuit a hard block, can't skip drafting for
  escalations, can't re-enter after human review. Every branch becomes an `if` inside a node,
  which hides control flow from the audit trail.
- *LLM supervisor/router.* An LLM chooses the next agent. This puts a non-deterministic
  component **in the safety path** — the one place the client said must be reliable. It's also
  the pattern most vulnerable to TCK-1013: an injected instruction that reaches a supervisor
  can redirect control flow itself. Rejected on principle, not on performance.
- *Bare async functions, no framework.* Genuinely viable and we'd lose little. LangGraph earns
  its place on exactly two features: `PostgresSaver` checkpointing and `interrupt()`, which make
  the human-in-the-loop pause/resume native rather than a state machine we hand-roll.

**Trade-off.** A framework dependency with a churn-prone API, and a reviewer has to know
LangGraph to read the code. Accepted because HITL pause/resume is a core requirement, not a
side feature.

---

### ADR-002 — The safety rule is code, not prompt

**Decision.** Hard blocks (money, account deletion, legal, security) are evaluated by a pure
Python `PolicyGate`. No LLM output can clear them.

**Rationale.** The client's hard requirement is absolute — "regardless of how confident the
system is". A rule expressed in a prompt is a rule the model can be argued out of, and
TCK-1013 is a live demonstration of someone trying. Prompts are probabilistic; the requirement
is not.

**Corollary — the monotonicity invariant.** The policy layer may only make a route **more**
conservative, never less:

```
auto_resolve  <  draft_for_review  <  escalate
```

Every agent can *demote* toward escalation; none can *promote* toward automation. This is a
single comparison enforced in one function, and it makes the safety property auditable by
reading ~10 lines rather than by trusting five prompts. It's the load-bearing idea of the
whole design — lead the README architecture section with it.

**Trade-off.** Lower automation rate. Deliberately accepted; the brief explicitly prefers it.

---

### ADR-003 — Confidence is a composite of four independent signals

**Decision.** Not model self-report alone. Combine: self-consistency across k samples,
model-reported confidence, token logprobs (`avg_logprobs` → `exp()`), and deterministic
heuristic penalties.

**Rationale.** Requirement 2 explicitly rejects "a fixed rule keyed only on category" and asks
where the signal comes from. Self-reported LLM confidence is known to be poorly calibrated and
uniformly high; a single source would be the weak answer.

**Combination rule: weighted geometric mean**, not arithmetic. A single very low signal must
drag the composite down — with an arithmetic mean, three confident signals mask one red flag.
Geometric mean makes any near-zero component dominate, which is the behaviour we want from a
safety-biased system.

**Trade-off.** k-sampling multiplies classifier cost. Mitigated by making k adaptive: k=1 by
default, re-sample only when the first result lands in the uncertainty band. See
the Confidence section of the main README.

---

### ADR-004 — Thresholds are swept against a gold set, not chosen by feel

**Decision.** Hand-label all 18 tickets, sweep `(tau_auto, tau_draft)` over a grid, pick the
pair maximising automation rate **subject to a hard constraint of zero safety violations**.
Publish the sweep table in the README.

**Rationale.** The brief asks "how you picked your thresholds". "0.85 felt right" is the weak
answer; a constrained optimisation with the frontier shown is the strong one.

**Trade-off.** 18 tickets is a tiny, self-labelled calibration set and will overfit. State this
plainly in the README along with what we'd do instead at scale (stratified sample of real
traffic, labelled by the support team, refreshed quarterly). An acknowledged limitation reads
as judgment; an unacknowledged one reads as a blind spot.

---

### ADR-005 — Four terminal actions, not three

**Decision.** Add `auto_resolve(clarification)` alongside auto-resolve / draft-for-review /
escalate.

**Rationale.** TCK-1009 ("it's broken" / "help"). A naive low-confidence-to-escalate rule sends
this to a human, but asking a clarifying question is both safe and fully automatable — and
low-signal tickets are high-volume in production. Conflating "I don't know what this is" with
"a human must handle this" floods the queue with exactly the work the system was bought to
remove.

**Trade-off.** A fourth action to explain and evaluate. Worth it — this is one of the sharper
observations available from the sample data.

---

### ADR-006 — Deterministic normalization before any LLM call

**Decision.** A pre-processing node handles quoted-printable decoding, mojibake stripping,
subject-chain cleanup, leetspeak normalization, dedup hashing, and injection scanning. No
model involved.

**Rationale.** TCK-1017 is solved by `quopri.decodestring()`. TCK-1014 is solved by a
similarity check against the store. Neither needs a token. Spending LLM calls on solved
problems is both wasteful and *less reliable*.

**Trade-off.** Hand-maintained heuristics that will need tuning against real mail. Acceptable —
they're cheap, testable without mocking an LLM, and fail visibly.

---

### ADR-007 — Prompt injection is handled structurally, in four layers

**Decision.** Delimited untrusted-data blocks → deterministic detection → policy floor →
over-determined routing. See [`TICKET-ANALYSIS.md`](TICKET-ANALYSIS.md) → TCK-1013.

**Rationale.** A prompt instructing the model to resist injection still leaves the model as the
thing deciding whether to comply. The only robust answer removes the model from that decision.
The fourth layer matters most: TCK-1013 is blocked by `refund_request` *independently* of every
injection defence, so the attack must defeat all four layers, not one.

**Trade-off.** Regex detection will false-positive on legitimate tickets quoting error text or
system messages. Since a false positive costs one unnecessary escalation and a false negative
costs an unauthorised refund, the asymmetry justifies a loose trigger. Log every fire so the
rate is measurable.

---

### ADR-008 — Two-tier model assignment

**Decision.** `gemini-3.1-flash-lite` for extraction/classification/drafting;
`gemini-3.7-flash` for the decision and critic agents.

**Rationale.** Spend on the two steps where an error is terminal — the route, and the last
check before a human sees a draft. Everything else is bounded, schema-constrained work whose
errors are caught downstream. Also makes the 500k/day cost section concrete.

**Trade-off.** Two model configs, two cache namespaces, two sets of prompt tuning. See
`PHASE0-FINDINGS.md` for the full cost table.

---

### ADR-009 — PostgreSQL with a normalized 7-table schema

**Decision.** Postgres (Neon in prod, `postgres:17-alpine` locally), seven tables, `JSONB` for
variable agent payloads. Alembic migrations. LangGraph checkpoints via `PostgresSaver` in the
same database.

**Rationale.** "Reconstruct exactly what happened and why" is a *query*, not a log grep. The
monitoring section of the README needs aggregations across entities at different grains — rule
fire rates, override rate by category, calibration curves. Those are joins with `GROUP BY`.

**Alternatives rejected:**
- *MongoDB.* The natural shape is one document per ticket with the trail embedded. Great for
  "show me ticket X", bad for every other question we need — those become `$unwind`-heavy
  pipelines or a hand-synced second collection. We optimise for the queries the brief's
  monitoring section demands.
- *SQLite.* Fine for a demo, and tempting. But we'd owe a "then we'd migrate to Postgres"
  paragraph in the scaling section anyway; starting on Postgres makes that section about
  partitioning and tiering (real engineering) rather than a migration we didn't do. Also: one
  writer, and we have concurrent workers.

**We keep document flexibility where it's genuinely needed.** Agent inputs/outputs vary by
agent and change as prompts evolve — those are `JSONB` with GIN indexes. Relational where we
join, schema-free where the shape is variable. That combination is why Postgres is the right
answer here rather than a compromise.

**Trade-off.** Docker becomes a prerequisite for local dev, and it's more setup than a file.
Mitigated by `docker compose up` doing everything in one command — which is a portfolio signal
in its own right (ADR-012).

### ADR-010 — Next.js review console alongside the CLI

**Decision.** Build both. CLI (Typer) satisfies requirement 3; Next.js makes the decision trail
legible.

**Rationale.** User-requested. The genuine argument for it: the audit trail is the deliverable
most likely to be under-appreciated in a JSON dump, and clicking through TCK-1013's four
defence layers firing in sequence is a far better demo than reading a results file.

**Trade-off.** The brief says a polished frontend is *explicitly out of scope*, and this adds
~2h to a 4–6h budget. Mitigations: the CLI ships first and independently, the UI stays at three
routes with no auth and no state library, and **the UI is the first thing cut if time runs
short**. Note in the README that it was built knowing it wasn't required, so it doesn't read as
a misread of the brief.

---

### ADR-011 — Both bonus items are in scope

**Decision.** Build the critic/reflection agent and the eval harness.

**Rationale.** Both fall out of the architecture anyway. The critic is a natural node in a graph
that already has conditional demotion; the eval harness is required to justify the thresholds in
ADR-004 regardless. The eval harness also *is* the answer to README topic 5 (monitoring drift) —
we get a section of the writeup for free.

**Trade-off.** ~1h combined. Cheapest points available in the whole assignment.

---

### ADR-012 — Ship the assignment first, then the portfolio version

**Decision.** Two tracks off one codebase. Track A (Phases 0–5, ~6h) is the graded submission.
Track B (Phases 6–8, +~6h) adds the Next.js console, deployment, and case study.

**Rationale.** The brief expects 4–6 focused hours and says explicitly that it evaluates
judgment, not endurance. A submission visibly reflecting 12 hours reads as poor scoping however
good it is. Submitting at the end of Phase 5 and continuing on a branch costs nothing and
removes the risk entirely.

**Trade-off.** The graded version won't have the UI that makes the audit trail most legible. If
you'd rather submit the whole thing, that's legitimate — but then **state the hours in the
README** and say the extra scope was deliberate. An unstated overrun looks like a misread
brief.

---

### ADR-013 — Public demo defaults to replay, not live inference

**Decision.** The deployed demo serves pre-computed traces for all 18 tickets from the
database. Live triage is available through one rate-limited box (per-IP and global caps) that
degrades to a message rather than an error when capped.

**Rationale.** A public endpoint calling Gemini on demand is an open wallet and a trivially
abusable injection surface — a particularly bad look on a project whose headline feature is
injection defence. Replay is also the *better* demo: instant, and it always shows the
interesting cases rather than whatever a visitor happened to type.

Human actions (approve/edit/reject) stay fully functional and write real rows; a nightly cron
truncates and re-seeds, so the console is genuinely interactive and never left broken by the
previous visitor.

**Trade-off.** Visitors don't see live model latency or variance. Acceptable — and the limits
are surfaced in the UI, since visibly having thought about abuse is itself a signal to a
technical reader.
