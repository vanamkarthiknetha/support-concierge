"""FastAPI review surface (requirement 3) and trail endpoint (requirement 4).

The Next.js console is a pure RENDERER of these endpoints. No triage logic lives in
the frontend -- if the UI needs a field the trail doesn't expose, the fix goes in
the backend, or the API and the CLI drift apart and the audit trail stops being a
single source of truth.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from concierge.config import get_settings
from concierge.store.repository import Repository

app = FastAPI(
    title="Support Concierge",
    description="Multi-agent support ticket triage with a fully auditable decision trail.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000",
                   "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

repo = Repository()


# --- request models ---------------------------------------------------------------


class ApproveIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=80)


class EditIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1)


class RejectIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=500)


# --- queues -------------------------------------------------------------------------


@app.get("/queues/{which}")
def get_queue(which: Literal["review", "escalate", "auto"]) -> dict[str, Any]:
    route = {
        "review": "draft_for_review",
        "escalate": "escalate",
        "auto": "auto_resolve",
    }[which]
    rows = repo.queue(route)
    return {"route": route, "count": len(rows), "items": rows}


@app.get("/tickets/{ticket_id}/trace")
def get_trace(ticket_id: str) -> dict[str, Any]:
    """The full decision trail. This is the endpoint that demonstrates requirement 4."""
    trail = repo.trail(ticket_id)
    if not trail:
        raise HTTPException(404, f"no such ticket: {ticket_id}")
    return trail


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict[str, Any]:
    trail = repo.trail(ticket_id)
    if not trail:
        raise HTTPException(404, f"no such ticket: {ticket_id}")
    return {
        "ticket": trail["ticket"],
        "decision": trail.get("decision"),
        "draft": trail.get("draft"),
        "reviews": trail.get("reviews", []),
    }


# --- human-in-the-loop actions --------------------------------------------------------


def _run_for(ticket_id: str) -> tuple[str, str | None]:
    trail = repo.trail(ticket_id)
    if not trail or not trail.get("run"):
        raise HTTPException(404, f"no run for ticket: {ticket_id}")
    return str(trail["run"]["id"]), (trail.get("draft") or {}).get("body")


@app.post("/tickets/{ticket_id}/approve")
def approve(ticket_id: str, body: ApproveIn) -> dict[str, Any]:
    run_id, draft = _run_for(ticket_id)
    rid = repo.record_review(
        ticket_id, run_id, body.reviewer, "approve",
        draft_before=draft, draft_after=draft,
    )
    # The send step is a deliberate no-op: outbound email is out of scope, and a
    # demo that silently mails real addresses would be worse than one that doesn't.
    return {"ok": True, "review_id": rid, "action": "approve", "would_send": bool(draft)}


@app.post("/tickets/{ticket_id}/edit")
def edit(ticket_id: str, body: EditIn) -> dict[str, Any]:
    run_id, draft = _run_for(ticket_id)
    rid = repo.record_review(
        ticket_id, run_id, body.reviewer, "edit",
        draft_before=draft, draft_after=body.body,
    )
    return {"ok": True, "review_id": rid, "action": "edit"}


@app.post("/tickets/{ticket_id}/reject")
def reject(ticket_id: str, body: RejectIn) -> dict[str, Any]:
    run_id, draft = _run_for(ticket_id)
    rid = repo.record_review(
        ticket_id, run_id, body.reviewer, "reject",
        draft_before=draft, reject_reason=body.reason,
    )
    return {"ok": True, "review_id": rid, "action": "reject"}


# --- monitoring -------------------------------------------------------------------------


@app.get("/stats")
def stats() -> dict[str, Any]:
    s = get_settings()
    counts = repo.counts()
    routes = repo.route_distribution()
    total = sum(r["n"] for r in routes) or 1
    auto = next(
        (r["n"] for r in routes if r["final_route"] == "auto_resolve"), 0
    )
    return {
        "counts": counts,
        "automation_rate": round(auto / total, 3),
        "route_distribution": routes,
        "policy_rule_fires": repo.rule_fire_counts(),
        "override_rate": repo.override_rate(),
        "calibration": repo.calibration(),
        "agent_failures": repo.failure_counts(),
        "config": {
            "models": {"cheap": s.model_cheap, "smart": s.model_smart},
            "tau_auto": s.tau_auto,
            "tau_draft": s.tau_draft,
            "config_hash": s.config_hash,
        },
    }


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        repo.counts()
        return {"ok": True, "db": "up"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"db down: {type(exc).__name__}") from exc
