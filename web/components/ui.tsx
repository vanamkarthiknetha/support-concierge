import Link from "next/link";
import type { Priority, Route } from "@/lib/types";

const ROUTE_META: Record<Route, { label: string; color: string; bg: string }> = {
  auto_resolve: {
    label: "auto-resolve",
    color: "text-auto",
    bg: "bg-auto/10 border-auto/30",
  },
  draft_for_review: {
    label: "draft for review",
    color: "text-draft",
    bg: "bg-draft/10 border-draft/30",
  },
  escalate: {
    label: "escalate",
    color: "text-escalate",
    bg: "bg-escalate/10 border-escalate/30",
  },
};

export function RouteBadge({ route, big }: { route: Route; big?: boolean }) {
  const m = ROUTE_META[route];
  return (
    <span
      className={`inline-flex items-center rounded border ${m.bg} ${m.color} mono ${
        big ? "px-3 py-1 text-sm font-semibold" : "px-2 py-0.5 text-[11px]"
      }`}
    >
      {m.label}
    </span>
  );
}

const PRIORITY_COLOR: Record<Priority, string> = {
  P0: "text-escalate border-escalate/40 bg-escalate/10",
  P1: "text-draft border-draft/40 bg-draft/10",
  P2: "text-muted border-edge",
  P3: "text-dim border-edge",
};

export function PriorityBadge({ priority }: { priority: Priority | null }) {
  if (!priority) return null;
  return (
    <span
      className={`mono text-[11px] px-1.5 py-0.5 rounded border ${PRIORITY_COLOR[priority]}`}
    >
      {priority}
    </span>
  );
}

export function Flag({
  kind,
  children,
}: {
  kind: "danger" | "warn" | "info";
  children: React.ReactNode;
}) {
  const cls = {
    danger: "text-escalate border-escalate/40 bg-escalate/10",
    warn: "text-draft border-draft/40 bg-draft/10",
    info: "text-accent border-accent/40 bg-accent/10",
  }[kind];
  return (
    <span className={`mono text-[10px] px-1.5 py-0.5 rounded border ${cls}`}>
      {children}
    </span>
  );
}

export function Stat({
  value,
  label,
  hint,
  tone = "default",
}: {
  value: string | number;
  label: string;
  hint?: string;
  tone?: "default" | "good" | "bad";
}) {
  const color =
    tone === "good" ? "text-auto" : tone === "bad" ? "text-escalate" : "text-text";
  return (
    <div className="panel p-4">
      <div className={`text-3xl font-semibold mono ${color}`}>{value}</div>
      <div className="text-sm text-muted mt-1">{label}</div>
      {hint && <div className="text-xs text-dim mt-1.5 leading-relaxed">{hint}</div>}
    </div>
  );
}

export function Section({
  title,
  subtitle,
  children,
  tone,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  tone?: "danger";
}) {
  return (
    <section
      className={`panel overflow-hidden ${
        tone === "danger" ? "border-escalate/40" : ""
      }`}
    >
      <div
        className={`px-4 py-2.5 border-b text-sm font-medium flex items-baseline gap-3 ${
          tone === "danger"
            ? "border-escalate/30 bg-escalate/5 text-escalate"
            : "border-edge bg-panel-2"
        }`}
      >
        <span>{title}</span>
        {subtitle && (
          <span className="text-xs text-dim font-normal mono">{subtitle}</span>
        )}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

export function TicketLink({ id }: { id: string }) {
  return (
    <Link href={`/tickets/${id}`} className="mono text-accent hover:underline">
      {id}
    </Link>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="panel p-8 text-center text-muted text-sm">{children}</div>
  );
}

export function BackendDown() {
  return (
    <div className="panel border-escalate/40 p-6">
      <div className="text-escalate font-medium mb-2">Backend unreachable</div>
      <p className="text-sm text-muted leading-relaxed">
        The API isn&apos;t responding. Start it with:
      </p>
      <pre className="mono text-xs mt-3 p-3 rounded bg-bg border border-edge text-muted">
        docker compose up -d{"\n"}
        cd backend && uv run uvicorn concierge.api.main:app --port 8010
      </pre>
    </div>
  );
}
