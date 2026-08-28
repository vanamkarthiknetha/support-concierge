import { getStats, tryGet } from "@/lib/api";
import { BackendDown, RouteBadge, Section, Stat } from "@/components/ui";

export const dynamic = "force-dynamic";

function Bar({ value, max, tone }: { value: number; max: number; tone: string }) {
  return (
    <div className="h-1.5 bg-bg rounded overflow-hidden">
      <div
        className={`h-full ${tone}`}
        style={{ width: `${max ? (value / max) * 100 : 0}%` }}
      />
    </div>
  );
}

export default async function StatsPage() {
  const stats = await tryGet(() => getStats());
  if (!stats) return <BackendDown />;

  const totalRoutes = stats.route_distribution.reduce((a, r) => a + Number(r.n), 0) || 1;
  const maxRule = Math.max(...stats.policy_rule_fires.map((r) => Number(r.n)), 1);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Monitoring</h1>
        <p className="text-sm text-muted mt-1">
          The queries here are why the audit trail is a relational store rather than
          one JSON blob per ticket — these are aggregations across entities at
          different grains.
        </p>
      </div>

      <div className="grid sm:grid-cols-4 gap-4">
        <Stat value={stats.counts.runs} label="runs" />
        <Stat value={stats.counts.steps} label="agent invocations" />
        <Stat
          value={stats.counts.dead_letters}
          label="dead-lettered"
          tone={stats.counts.dead_letters > 0 ? "bad" : "good"}
          hint="Tickets that hit an unhandled error — escalated, never dropped"
        />
        <Stat
          value={`${Math.round(stats.automation_rate * 100)}%`}
          label="automation rate"
        />
      </div>

      <div className="grid lg:grid-cols-2 gap-5">
        <Section title="Route distribution">
          <div className="space-y-3">
            {stats.route_distribution.map((r) => (
              <div key={r.final_route}>
                <div className="flex items-center gap-2 mb-1.5">
                  <RouteBadge route={r.final_route} />
                  <span className="mono text-sm ml-auto">{r.n}</span>
                  <span className="mono text-xs text-dim w-12 text-right">
                    {Math.round((Number(r.n) / totalRoutes) * 100)}%
                  </span>
                </div>
                <Bar
                  value={Number(r.n)}
                  max={totalRoutes}
                  tone={
                    r.final_route === "auto_resolve"
                      ? "bg-auto"
                      : r.final_route === "draft_for_review"
                        ? "bg-draft"
                        : "bg-escalate"
                  }
                />
              </div>
            ))}
          </div>
        </Section>

        <Section
          title="Policy rule fires"
          subtitle="a DROP here means classification drifted, not that risk vanished"
        >
          {stats.policy_rule_fires.length === 0 ? (
            <p className="text-sm text-muted">No rules fired yet.</p>
          ) : (
            <div className="space-y-2.5">
              {stats.policy_rule_fires.map((r) => (
                <div key={r.rule_id}>
                  <div className="flex items-baseline gap-2 mb-1">
                    <span className="mono text-xs text-escalate">{r.rule_id}</span>
                    <span className="mono text-xs ml-auto">{r.n}</span>
                  </div>
                  <Bar value={Number(r.n)} max={maxRule} tone="bg-escalate/60" />
                </div>
              ))}
            </div>
          )}
        </Section>

        <Section
          title="Human override rate"
          subtitle="the best ground-truth quality signal, and it is free"
        >
          {stats.override_rate.every((r) => Number(r.reviewed) === 0) ? (
            <p className="text-sm text-muted leading-relaxed">
              No human reviews recorded yet. Approve, edit, or reject something in
              the review queue and it appears here — reviewers rejecting drafts{" "}
              <em>is</em> the quality metric.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-xs text-dim">
                <tr className="text-left">
                  <th className="pb-2">route</th>
                  <th className="pb-2">reviewed</th>
                  <th className="pb-2">rejected</th>
                  <th className="pb-2">edited</th>
                  <th className="pb-2">reject rate</th>
                </tr>
              </thead>
              <tbody className="mono text-xs">
                {stats.override_rate.map((r) => (
                  <tr key={r.final_route} className="border-t border-edge">
                    <td className="py-2">{r.final_route}</td>
                    <td>{r.reviewed}</td>
                    <td>{r.rejected}</td>
                    <td>{r.edited}</td>
                    <td>{r.reject_rate ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>

        <Section
          title="Agent failures"
          subtitle="failed attempts are rows, not silence"
        >
          {stats.agent_failures.length === 0 ? (
            <p className="text-sm text-muted">No agent failures recorded.</p>
          ) : (
            <table className="w-full text-sm mono text-xs">
              <tbody>
                {stats.agent_failures.map((f, i) => (
                  <tr key={i} className="border-t border-edge">
                    <td className="py-2">{f.agent}</td>
                    <td className="text-escalate">{f.error_type}</td>
                    <td className="text-right">{f.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>
      </div>

      <Section title="Configuration" subtitle="recorded on every run, for replay">
        <div className="mono text-xs text-muted space-y-1">
          <div>cheap model: {stats.config.models.cheap}</div>
          <div>smart model: {stats.config.models.smart}</div>
          <div>
            tau_auto: {stats.config.tau_auto} · tau_draft: {stats.config.tau_draft}
          </div>
          <div>config_hash: {stats.config.config_hash}</div>
        </div>
      </Section>
    </div>
  );
}
