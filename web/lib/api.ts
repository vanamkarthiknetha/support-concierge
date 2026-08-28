import type { QueueResponse, Stats, Trace } from "./types";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";

class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!res.ok) {
    throw new ApiError(`GET ${path} failed: ${res.status}`, res.status);
  }
  return res.json() as Promise<T>;
}

export async function getQueue(which: "review" | "escalate" | "auto") {
  return get<QueueResponse>(`/queues/${which}`);
}

export async function getTrace(ticketId: string) {
  return get<Trace>(`/tickets/${ticketId}/trace`);
}

export async function getStats() {
  return get<Stats>("/stats");
}

/** Returns null instead of throwing, so a page can render a "backend is down"
 *  state rather than a Next.js error overlay. */
export async function tryGet<T>(fn: () => Promise<T>): Promise<T | null> {
  try {
    return await fn();
  } catch {
    return null;
  }
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new ApiError(`POST ${path} failed: ${res.status}`, res.status);
  }
  return res.json() as Promise<T>;
}

export async function approve(ticketId: string, reviewer: string) {
  return post<{ ok: boolean }>(`/tickets/${ticketId}/approve`, { reviewer });
}

export async function editDraft(ticketId: string, reviewer: string, body: string) {
  return post<{ ok: boolean }>(`/tickets/${ticketId}/edit`, { reviewer, body });
}

export async function reject(ticketId: string, reviewer: string, reason: string) {
  return post<{ ok: boolean }>(`/tickets/${ticketId}/reject`, { reviewer, reason });
}

export { API_BASE };
const API_BASE = API;
