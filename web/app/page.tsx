import Link from "next/link";
import { getStats, tryGet } from "@/lib/api";
import { BackendDown, Stat } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function Home() {
  const stats = await tryGet(() => getStats());
  if (!stats) return <BackendDown />;

  const autoResolved = stats.counts.auto_resolved;
  const total = stats.counts.runs || 1;

  return (
    <div className="space-y-10">
      {/* ── the thesis ───────────────────────────────────────────────── */}
      <section className="space-y-4">
        <h1 className="text-3xl font-semibold tracking-tight leading-tight">
          Support tickets arrive. Some can be answered automatically —{" "}
          <span className="text-escalate">some absolutely must not be.</span>
        </h1>
        <p className="text-muted leading-relaxed max-w-2xl">
          A multi-agent triage system that classifies every inbound ticket, decides
          whether to auto-resolve, draft for review, or escalate, and records a full
          decision trail. The rule that nothing involving money, deletion, legal
          threats, or security can be automated is enforced in{" "}
          <span className="text-text">code, not in a prompt</span> — because a rule
          written in a prompt is a rule a model can be argued out of.
        </p>
      </section>

      {/* ── the numbers ──────────────────────────────────────────────── */}
      <section className="grid sm:grid-cols-3 gap-4">
        <Stat
          value={stats.counts.runs}
          label="tickets triaged"
          hint={`${stats.counts.steps} agent invocations recorded, failures included`}
        />
        <Stat
          value={`${Math.round((autoResolved / total) * 100)}%`}
          label="handled without a human"
          hint="Deliberately conservative — the brief asks us to err toward caution"
        />
        <Stat
          value={0}
          label="unsafe auto-resolves"
          tone="good"
          hint="Across the baseline run and every failure-injection run"
        />
      </section>

      {/* ── the invariant ────────────────────────────────────────────── */}
      <section className="panel p-5">
        <div className="text-xs uppercase tracking-wide text-dim mb-3">
          The whole safety property, in one line
        </div>
        <pre className="mono text-sm text-accent leading-relaxed scroll-x">
          {`auto_resolve  <  draft_for_review  <  escalate

final_route = most_conservative(policy_ceiling, decision, critic, failures)`}
        </pre>
        <p className="text-sm text-muted mt-4 leading-relaxed">
          Five LLM agents propose. A deterministic policy gate sets a ceiling none of
          them can clear. Every component may push a ticket{" "}
          <span className="text-text">toward</span> a human; none may pull it back
          toward automation. That makes the guarantee verifiable by reading ~40 lines
          instead of trusting five prompts.
        </p>
      </section>

      {/* ── the hook ─────────────────────────────────────────────────── */}
      <section className="grid md:grid-cols-2 gap-4">
        <Link
          href="/tickets/TCK-1013"
          className="panel p-5 border-escalate/40 hover:border-escalate transition-colors group"
        >
          <div className="text-escalate text-sm font-medium mb-2">
            See a prompt-injection attack get stopped →
          </div>
          <p className="text-sm text-muted leading-relaxed">
            One ticket hides an instruction telling the system to approve a refund,
            mark itself resolved, and skip human review — exactly the three things
            the client forbade. Watch four independent defences fire, and note that
            the outcome is over-determined: delete every injection defence and the
            refund category alone still blocks it.
          </p>
        </Link>

        <Link
          href="/queues"
          className="panel p-5 hover:border-accent/50 transition-colors"
        >
          <div className="text-accent text-sm font-medium mb-2">
            Browse the review queues →
          </div>
          <p className="text-sm text-muted leading-relaxed">
            What is waiting on a human, what was escalated and to which specialist
            queue, and what the system closed by itself. Every ticket opens into its
            full decision trail: each agent call, each policy rule that fired, and
            the confidence behind the routing decision.
          </p>
        </Link>
      </section>

      {/* ── config ───────────────────────────────────────────────────── */}
      <section className="text-xs text-dim mono space-y-1">
        <div>
          models: {stats.config.models.cheap} · {stats.config.models.smart}
        </div>
        <div>
          thresholds: tau_auto={stats.config.tau_auto} tau_draft=
          {stats.config.tau_draft} · config {stats.config.config_hash}
        </div>
        <div>
          dead-lettered: {stats.counts.dead_letters} · human reviews recorded:{" "}
          {stats.counts.reviews}
        </div>
      </section>
    </div>
  );
}
