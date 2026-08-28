"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { approve, editDraft, reject } from "@/lib/api";

/** Approve / edit / reject — the human-in-the-loop surface (requirement 3).
 *  The reviewer name is a localStorage convenience, not auth: this is a
 *  prototype, and pretending otherwise would be worse than being explicit. */
export function ReviewActions({
  ticketId,
  body,
}: {
  ticketId: string;
  body: string;
}) {
  const router = useRouter();
  const [reviewer, setReviewer] = useState("");
  const [text, setText] = useState(body);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    try {
      setReviewer(localStorage.getItem("concierge.reviewer") ?? "");
    } catch {
      /* private mode, blocked storage — the field just starts empty */
    }
  }, []);

  function remember(name: string) {
    setReviewer(name);
    try {
      localStorage.setItem("concierge.reviewer", name);
    } catch {
      /* ignore */
    }
  }

  async function act(kind: "approve" | "edit" | "reject") {
    if (!reviewer.trim()) {
      setError("Enter your name first — reviews are attributed in the audit trail.");
      return;
    }
    if (kind === "reject" && !reason.trim()) {
      setError("A rejection needs a reason. It is the system's best quality signal.");
      return;
    }
    setBusy(kind);
    setError(null);
    try {
      if (kind === "approve") await approve(ticketId, reviewer);
      if (kind === "edit") await editDraft(ticketId, reviewer, text);
      if (kind === "reject") await reject(ticketId, reviewer, reason);
      setDone(kind);
      router.refresh();
    } catch {
      setError("Request failed — is the API running?");
    } finally {
      setBusy(null);
    }
  }

  if (done) {
    return (
      <div className="mt-4 pt-3 border-t border-edge text-sm text-auto">
        Recorded <span className="mono">{done}</span> as{" "}
        <span className="mono">{reviewer}</span>. Sending is a logged no-op in this
        prototype.
      </div>
    );
  }

  const edited = text !== body;

  return (
    <div className="mt-4 pt-4 border-t border-edge space-y-3">
      <input
        value={reviewer}
        onChange={(e) => remember(e.target.value)}
        placeholder="your name (recorded against this decision)"
        className="w-full bg-bg border border-edge rounded px-3 py-2 text-sm
                   focus:border-accent/60 focus:outline-none"
      />

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={8}
        className="w-full bg-bg border border-edge rounded px-3 py-2 mono text-xs
                   leading-relaxed focus:border-accent/60 focus:outline-none"
      />

      <input
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="rejection reason (required to reject)"
        className="w-full bg-bg border border-edge rounded px-3 py-2 text-sm
                   focus:border-escalate/60 focus:outline-none"
      />

      {error && <p className="text-xs text-escalate">{error}</p>}

      <div className="flex gap-2 flex-wrap">
        <button
          onClick={() => act(edited ? "edit" : "approve")}
          disabled={busy !== null}
          className="px-3 py-1.5 rounded border border-auto/40 bg-auto/10 text-auto
                     text-sm hover:bg-auto/20 disabled:opacity-50 transition-colors"
        >
          {busy ? "…" : edited ? "Approve with edits" : "Approve as-is"}
        </button>
        <button
          onClick={() => act("reject")}
          disabled={busy !== null}
          className="px-3 py-1.5 rounded border border-escalate/40 bg-escalate/10
                     text-escalate text-sm hover:bg-escalate/20 disabled:opacity-50
                     transition-colors"
        >
          Reject
        </button>
        {edited && (
          <span className="text-xs text-draft self-center">
            edited — both versions are stored
          </span>
        )}
      </div>
    </div>
  );
}
