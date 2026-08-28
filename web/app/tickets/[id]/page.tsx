import Link from "next/link";
import { getTrace, tryGet } from "@/lib/api";
import type { AgentStep, PolicyEvent, Trace } from "@/lib/types";
import {
  BackendDown,
  Flag,
  PriorityBadge,
  RouteBadge,
  Section,
  TicketLink,
} from "@/components/ui";
import { ReviewActions } from "./review-actions";

export const dynamic = "force-dynamic";

function fmt(n: number | null | undefined, digits = 2) {
  return n === null || n === undefined ? "—" : n.toFixed(digits);
}

/** Highlights the quarantine marker so the removal is visible, not silent. */
function QuarantinedBody({ body }: { body: string }) {
  const parts = body.split(/(\[CONTENT REMOVED: suspected injected instruction\])/g);
  return (
    <pre className="mono text-xs leading-relaxed text-muted">
      {parts.map((p, i) =>
        p.startsWith("[CONTENT REMOVED") ? (
          <span
            key={i}
            className="bg-escalate/15 text-escalate border border-escalate/40 rounded px-1"
          >
            {p}
          </span>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </pre>
  );
}

function StepRow({ step }: { step: AgentStep }) {
  const failed = step.error_type !== null;
  return (
    <div
      className={`border rounded p-3 ${
        failed ? "border-escalate/40 bg-escalate/5" : "border-edge bg-panel-2"
      }`}
    >
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="mono text-dim">#{step.seq}</span>
        <span className="font-medium">{step.agent}</span>
        {step.model_id && (
          <span className="mono text-dim">{step.model_id}</span>
        )}
        {step.latency_ms !== null && (
          <span className="mono text-dim">{step.latency_ms}ms</span>
        )}
        {step.attempt > 1 && <Flag kind="warn">attempt {step.attempt}</Flag>}
        {failed && <Flag kind="danger">{step.error_type}</Flag>}
        {step.output_tokens !== null && (
          <span className="mono text-dim ml-auto">
            {step.input_tokens ?? "?"}→{step.output_tokens} tok
          </span>
        )}
      </div>

      {failed && step.error_detail && (
        <p className="mt-2 text-xs text-escalate/90 mono">{step.error_detail}</p>
      )}

      {step.parsed_output && (
        <details className="mt-2">
          <summary className="text-xs text-dim cursor-pointer hover:text-muted">
            output
          </summary>
          <pre className="mono text-[11px] mt-2 p-2 rounded bg-bg border border-edge text-muted scroll-x">
            {JSON.stringify(step.parsed_output, null, 2)}
          </pre>
        </details>
      )}

      {step.prompt_hash && (
        <div className="mt-1.5 text-[10px] text-dim mono">
          prompt {step.prompt_hash}
        </div>
      )}
    </div>
  );
}

function PolicyRow({ e }: { e: PolicyEvent }) {
  return (
    <div className="border border-escalate/30 bg-escalate/5 rounded p-3">
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="mono text-escalate font-medium">{e.rule_id}</span>
        <span className="text-dim mono">v{e.rule_version}</span>
        <span className="ml-auto mono text-dim">
          {e.route_before} → <span className="text-escalate">{e.route_after}</span>
        </span>
      </div>
      {e.triggered_by && (
        <div className="mt-1.5 text-xs text-muted mono">{e.triggered_by}</div>
      )}
      {e.detail && (
        <p className="mt-1.5 text-xs text-dim leading-relaxed">{e.detail}</p>
      )}
    </div>
  );
}

export default async function TicketPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const trace = await tryGet<Trace>(() => getTrace(id));

  if (!trace) return <BackendDown />;

  const { ticket, decision, draft, steps, policy_events, reviews, run } = trace;
  const wasRepaired =
    ticket.subject_norm !== ticket.subject_raw ||
    ticket.body_norm !== ticket.body_raw;

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3 text-sm">
        <Link href="/queues" className="text-muted hover:text-text">
          ← queues
        </Link>
        <span className="mono text-dim">{id}</span>
        {run && (
          <span className="mono text-[11px] text-dim ml-auto">
            graph {run.graph_version} · config {run.config_hash}
          </span>
        )}
      </div>

      {/* ── the answer, first ─────────────────────────────────────────── */}
      {decision && (
        <div className="panel p-5">
          <div className="flex items-center gap-3 flex-wrap">
            <RouteBadge route={decision.final_route} big />
            <PriorityBadge priority={decision.priority} />
            {decision.escalation_queue && (
              <span className="mono text-xs text-muted">
                → {decision.escalation_queue} queue
              </span>
            )}
            {ticket.injection_suspected && <Flag kind="danger">INJECTION</Flag>}
            {ticket.is_followup && <Flag kind="warn">FOLLOW-UP</Flag>}
            {ticket.language !== "en" && (
              <Flag kind="info">lang: {ticket.language}</Flag>
            )}
          </div>

          <div className="mt-4 border-l-2 border-accent/50 pl-3">
            <div className="text-[11px] uppercase tracking-wide text-dim mb-1">
              Why wasn&apos;t this auto-resolved?
            </div>
            <p className="text-sm text-text leading-relaxed">
              {decision.binding_constraint}
            </p>
          </div>

          {decision.rationale && (
            <p className="mt-3 text-xs text-muted leading-relaxed">
              <span className="text-dim">decision agent: </span>
              {decision.rationale}
            </p>
          )}
        </div>
      )}

      {/* ── the ticket ─────────────────────────────────────────────────── */}
      <Section
        title={ticket.subject_raw || "(no subject)"}
        subtitle={`${ticket.from_name ?? "unknown"} <${ticket.from_email ?? "?"}>`}
      >
        <pre className="mono text-xs leading-relaxed text-muted">
          {ticket.body_raw}
        </pre>
      </Section>

      {/* ── normalization ──────────────────────────────────────────────── */}
      {wasRepaired && (
        <Section
          title="Normalized"
          subtitle={`deterministic · no LLM · quality ${fmt(ticket.text_quality)}`}
        >
          <div className="flex gap-1.5 flex-wrap mb-3">
            {ticket.repairs.map((r) => (
              <Flag key={r} kind="info">
                {r}
              </Flag>
            ))}
          </div>
          <QuarantinedBody body={ticket.body_norm ?? ""} />
          {ticket.related_tickets.length > 0 && (
            <div className="mt-3 text-xs text-muted">
              related:{" "}
              {ticket.related_tickets.map((t) => (
                <span key={t} className="mr-2">
                  <TicketLink id={t} />
                </span>
              ))}
            </div>
          )}
        </Section>
      )}

      {/* ── injection ──────────────────────────────────────────────────── */}
      {ticket.injection_suspected && (
        <Section
          title="Prompt injection detected"
          subtitle="quarantined before any agent saw it"
          tone="danger"
        >
          <p className="text-xs text-muted leading-relaxed mb-3">
            This ticket contains text shaped like instructions aimed at the system.
            It was removed by a deterministic scanner before the first LLM call, and
            the policy layer forced escalation independently of anything a model
            said. The spans below exist only in this audit record — they were never
            re-inserted into a prompt.
          </p>
          <div className="space-y-2">
            {ticket.injection_spans.map((s, i) => (
              <div
                key={i}
                className="border border-escalate/30 rounded p-2.5 bg-bg"
              >
                <div className="mono text-[11px] text-escalate mb-1">
                  {s.pattern}
                </div>
                <div className="mono text-xs text-muted">{s.text}</div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* ── policy events ──────────────────────────────────────────────── */}
      {policy_events.length > 0 && (
        <Section
          title="Policy gate"
          subtitle={`deterministic · ${policy_events.length} rule${
            policy_events.length > 1 ? "s" : ""
          } fired`}
          tone="danger"
        >
          <p className="text-xs text-dim mb-3 leading-relaxed">
            Every rule that fired is recorded, not just the first match — so if one
            rule is later found miscalibrated, the trail still shows whether the
            ticket was independently covered.
          </p>
          <div className="space-y-2">
            {policy_events.map((e) => (
              <PolicyRow key={e.id} e={e} />
            ))}
          </div>
        </Section>
      )}

      {/* ── confidence ─────────────────────────────────────────────────── */}
      {decision && (
        <Section
          title="Confidence"
          subtitle={`geometric mean · thresholds in force: auto ${decision.tau_auto} / draft ${decision.tau_draft}`}
        >
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
            {[
              ["margin", decision.c_margin, "top1 − top2 (primary)"],
              ["cross-model", decision.c_crossmodel, "cheap vs smart (adaptive)"],
              ["self-report", decision.c_selfreport, "model's own score (weak)"],
              ["composite", decision.composite, "after penalties"],
            ].map(([label, val, hint]) => (
              <div key={label as string} className="bg-panel-2 rounded p-3">
                <div className="mono text-xl">{fmt(val as number | null)}</div>
                <div className="text-xs text-muted mt-0.5">{label as string}</div>
                <div className="text-[10px] text-dim mt-1">{hint as string}</div>
              </div>
            ))}
          </div>
          {Object.keys(decision.penalties).length > 0 && (
            <div>
              <div className="text-xs text-dim mb-2">
                deterministic penalties — the only signals that can detect{" "}
                <em>missing</em> evidence
              </div>
              <div className="flex gap-1.5 flex-wrap">
                {Object.entries(decision.penalties).map(([k, v]) => (
                  <Flag key={k} kind="warn">
                    {k} −{v}
                  </Flag>
                ))}
              </div>
            </div>
          )}
          <div className="mt-4 pt-3 border-t border-edge text-xs text-dim mono">
            ceiling {decision.policy_ceiling} · proposed {decision.proposed_route} ·
            critic {decision.critic_verdict ?? "—"} ·{" "}
            <span className="text-text">final {decision.final_route}</span>
          </div>
        </Section>
      )}

      {/* ── agent steps ────────────────────────────────────────────────── */}
      <Section
        title="Agent steps"
        subtitle={`${steps.length} invocations, failures included`}
      >
        <div className="space-y-2">
          {steps.map((s) => (
            <StepRow key={s.id} step={s} />
          ))}
        </div>
      </Section>

      {/* ── draft + critique ───────────────────────────────────────────── */}
      {draft && (
        <Section
          title={draft.send ? "Drafted reply" : "Close without reply"}
          subtitle={draft.template_id ?? "generated"}
        >
          {draft.send ? (
            <pre className="mono text-xs leading-relaxed whitespace-pre-wrap">
              {draft.body}
            </pre>
          ) : (
            <p className="text-sm text-muted">
              No reply will be sent. Replying to this ticket would be wrong —
              sending a canned response to unsolicited spam is a bug, not a courtesy.
            </p>
          )}

          {draft.critique && (
            <div className="mt-4 pt-3 border-t border-edge">
              <div className="text-xs text-dim mb-2">
                review agent — may demote the route, never promote it
              </div>
              <div className="flex gap-1.5 flex-wrap mb-2">
                {draft.critique.promises_forbidden_action && (
                  <Flag kind="danger">promises forbidden action</Flag>
                )}
                {draft.critique.asserts_unsupported_facts && (
                  <Flag kind="warn">unsupported facts</Flag>
                )}
                {draft.critique.language_mismatch && (
                  <Flag kind="warn">language mismatch</Flag>
                )}
                {!draft.critique.tone_appropriate && (
                  <Flag kind="warn">tone</Flag>
                )}
                {draft.critique.demote_to && (
                  <Flag kind="danger">demoted → {draft.critique.demote_to}</Flag>
                )}
                {draft.critique.approved &&
                  !draft.critique.demote_to && <Flag kind="info">approved</Flag>}
              </div>
              {draft.critique.issues.length > 0 && (
                <ul className="text-xs text-muted space-y-1 list-disc pl-4">
                  {draft.critique.issues.map((i, n) => (
                    <li key={n}>{i}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {decision?.final_route === "draft_for_review" && (
            <ReviewActions ticketId={id} body={draft.body ?? ""} />
          )}
        </Section>
      )}

      {/* ── human actions ──────────────────────────────────────────────── */}
      {reviews.length > 0 && (
        <Section title="Human review" subtitle={`${reviews.length} action(s)`}>
          <div className="space-y-2">
            {reviews.map((r) => (
              <div key={r.id} className="text-xs flex items-center gap-2">
                <Flag
                  kind={
                    r.action === "reject"
                      ? "danger"
                      : r.action === "edit"
                        ? "warn"
                        : "info"
                  }
                >
                  {r.action}
                </Flag>
                <span className="text-muted">{r.reviewer}</span>
                <span className="text-dim mono">
                  {new Date(r.acted_at).toLocaleString()}
                </span>
                {r.reject_reason && (
                  <span className="text-muted">— {r.reject_reason}</span>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
