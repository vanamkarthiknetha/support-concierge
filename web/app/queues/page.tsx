import Link from "next/link";
import { getQueue, tryGet } from "@/lib/api";
import type { QueueItem, QueueResponse } from "@/lib/types";
import { BackendDown, Empty, Flag, PriorityBadge, RouteBadge } from "@/components/ui";

export const dynamic = "force-dynamic";

function Card({ item }: { item: QueueItem }) {
  return (
    <Link
      href={`/tickets/${item.ticket_id}`}
      className="block panel p-3.5 hover:border-accent/40 transition-colors"
    >
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <span className="mono text-xs text-accent">{item.ticket_id}</span>
        <PriorityBadge priority={item.priority} />
        {item.injection_suspected && <Flag kind="danger">INJECTION</Flag>}
        {item.is_followup && <Flag kind="warn">FOLLOW-UP</Flag>}
        {item.language && item.language !== "en" && (
          <Flag kind="info">{item.language}</Flag>
        )}
        {item.review_count > 0 && <Flag kind="info">reviewed</Flag>}
        <span className="ml-auto mono text-xs text-dim">
          {item.composite !== null ? item.composite.toFixed(2) : "—"}
        </span>
      </div>

      <div className="text-sm font-medium mb-1 truncate">
        {item.subject_raw || "(no subject)"}
      </div>
      <div className="text-xs text-dim mb-2">
        {item.from_name ?? "unknown"}
        {item.escalation_queue && (
          <span className="text-muted"> · → {item.escalation_queue}</span>
        )}
      </div>

      <p className="text-xs text-muted leading-relaxed line-clamp-2">
        {item.binding_constraint}
      </p>
    </Link>
  );
}

function Column({
  title,
  hint,
  data,
}: {
  title: string;
  hint: string;
  data: QueueResponse | null;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <h2 className="font-medium">{title}</h2>
        <span className="mono text-sm text-dim">{data?.count ?? 0}</span>
      </div>
      <p className="text-xs text-dim leading-relaxed">{hint}</p>
      {!data || data.items.length === 0 ? (
        <Empty>Nothing here.</Empty>
      ) : (
        <div className="space-y-2.5">
          {data.items.map((i) => (
            <Card key={i.ticket_id} item={i} />
          ))}
        </div>
      )}
    </div>
  );
}

export default async function QueuesPage() {
  const [review, escalate, auto] = await Promise.all([
    tryGet(() => getQueue("review")),
    tryGet(() => getQueue("escalate")),
    tryGet(() => getQueue("auto")),
  ]);

  if (!review && !escalate && !auto) return <BackendDown />;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Work queues</h1>
        <p className="text-sm text-muted mt-1">
          Everything a human needs to act on, plus what the system handled by itself.
        </p>
      </div>

      <div className="grid lg:grid-cols-3 gap-6">
        <Column
          title="Needs review"
          hint="A reply was drafted but not sent. A human approves, edits, or rejects it."
          data={review}
        />
        <Column
          title="Escalated"
          hint="Handed to a human with no auto-generated reply, and routed to the right specialist queue."
          data={escalate}
        />
        <Column
          title="Auto-resolved"
          hint="Answered and closed without a human. Shown here so the automated path is auditable too."
          data={auto}
        />
      </div>

      <div className="flex items-center gap-2 flex-wrap text-xs text-dim pt-2">
        <RouteBadge route="auto_resolve" />
        <RouteBadge route="draft_for_review" />
        <RouteBadge route="escalate" />
        <span className="ml-2">
          Every component can push a ticket toward human review; nothing can pull it
          back toward automation.
        </span>
      </div>
    </div>
  );
}
