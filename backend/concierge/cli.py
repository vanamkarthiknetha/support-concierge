"""Command-line interface.

`concierge run` triages tickets; `concierge queue` / `show` / `approve` / `edit` /
`reject` are the human-in-the-loop surface (requirement 3). The CLI alone satisfies
that requirement -- the web console is additive.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from concierge.agents.agents import Agents
from concierge.config import get_settings, results_dir, sample_tickets_path
from concierge.graph.build import TriageRunner
from concierge.llm.client import LLMClient
from concierge.llm.faults import FaultInjector
from concierge.models import Ticket, TriageState
from concierge.store.repository import Repository

app = typer.Typer(add_completion=False, help="Support Concierge — multi-agent ticket triage")
console = Console()

ROUTE_STYLE = {
    "auto_resolve": "green",
    "draft_for_review": "yellow",
    "escalate": "red",
}


def load_tickets(path: Path) -> list[Ticket]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Ticket(
            id=t["id"],
            received_at=datetime.fromisoformat(t["received_at"].replace("Z", "+00:00")),
            from_name=t.get("from_name"),
            from_email=t.get("from_email"),
            subject=t.get("subject", ""),
            body=t.get("body", ""),
        )
        for t in raw
    ]


def state_to_dict(state: TriageState) -> dict:
    return json.loads(state.model_dump_json())


@app.command()
def initdb(reset: bool = typer.Option(False, help="Truncate all tables first")) -> None:
    """Create the audit schema (and optionally wipe it)."""
    repo = Repository()
    repo.init_schema()
    if reset:
        repo.reset()
        console.print("[yellow]tables truncated[/]")
    console.print("[green]schema ready[/]")
    console.print(repo.counts())


@app.command()
def run(
    tickets: Path | None = typer.Option(None, help="Path to tickets JSON"),
    out: Path | None = typer.Option(None, help="Write results JSON here"),
    chaos: list[str] = typer.Option(
        [], "--chaos", help="Inject faults: agent=classifier,mode=timeout,rate=1.0"
    ),
    limit: int | None = typer.Option(None, help="Only process the first N tickets"),
    reset: bool = typer.Option(False, help="Wipe the store before running"),
    persist: bool = typer.Option(True, help="Write the audit trail to Postgres"),
) -> None:
    """Triage tickets end to end."""
    s = get_settings()
    path = tickets or sample_tickets_path()
    all_tickets = load_tickets(path)
    if limit:
        all_tickets = all_tickets[:limit]

    repo = Repository()
    if persist:
        repo.init_schema()
        if reset:
            repo.reset()

    faults = FaultInjector.parse(chaos)
    llm = LLMClient(faults=faults)
    runner = TriageRunner(Agents(llm), lookup=repo if persist else None, repo=repo)

    console.print(
        Panel(
            f"tickets: [bold]{len(all_tickets)}[/]   "
            f"models: {s.model_cheap} / {s.model_smart}\n"
            f"thresholds: tau_auto={s.tau_auto} tau_draft={s.tau_draft}   "
            f"config_hash: {s.config_hash}\n"
            f"chaos: [bold]{faults.describe()}[/]",
            title="Support Concierge",
        )
    )

    results = []
    table = Table("ticket", "route", "conf", "queue", "why", box=None)

    for t in all_tickets:
        state = runner.run(t)
        if persist:
            repo.save_ticket(t, state)
            repo.save_run(state)
        results.append(state_to_dict(state))

        route = state.final.route.value
        conf = f"{state.confidence.composite:.2f}" if state.confidence else "-"
        table.add_row(
            t.id,
            f"[{ROUTE_STYLE[route]}]{route}[/]",
            conf,
            state.final.queue.value if state.final.queue else "-",
            state.final.binding_constraint[:62],
        )
        console.print(
            f"  {t.id}  [{ROUTE_STYLE[route]}]{route:<17}[/] conf={conf}  "
            f"steps={len(state.steps)} failures={len(state.failures)}"
        )

    console.print()
    console.print(table)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["final"]["route"]] = counts.get(r["final"]["route"], 0) + 1
    console.print(f"\n[bold]summary:[/] {counts}")

    dest = out or (results_dir() / "baseline.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "config_hash": s.config_hash,
                "models": {"cheap": s.model_cheap, "smart": s.model_smart},
                "thresholds": {"tau_auto": s.tau_auto, "tau_draft": s.tau_draft},
                "chaos": faults.describe(),
                "summary": counts,
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    console.print(f"[green]wrote[/] {dest}")


@app.command()
def queue(
    which: str = typer.Argument("review", help="review | escalate | auto"),
) -> None:
    """Show a work queue."""
    route = {
        "review": "draft_for_review",
        "escalate": "escalate",
        "auto": "auto_resolve",
    }.get(which, which)

    rows = Repository().queue(route)
    if not rows:
        console.print(f"[dim]{route} queue is empty[/]")
        return

    table = Table(title=f"{route} ({len(rows)})")
    for col in ("ticket", "pri", "from", "subject", "conf", "queue", "why"):
        table.add_column(col, overflow="fold")
    for r in rows:
        flags = ""
        if r["injection_suspected"]:
            flags += " [red]INJ[/]"
        if r["is_followup"]:
            flags += " [yellow]DUP[/]"
        table.add_row(
            r["ticket_id"] + flags,
            r["priority"] or "-",
            (r["from_name"] or "")[:16],
            (r["subject_raw"] or "")[:34],
            f"{r['composite']:.2f}" if r["composite"] is not None else "-",
            r["escalation_queue"] or "-",
            (r["binding_constraint"] or "")[:52],
        )
    console.print(table)


@app.command()
def show(ticket_id: str) -> None:
    """Render one ticket's full decision trail."""
    trail = Repository().trail(ticket_id)
    if not trail:
        console.print(f"[red]no such ticket:[/] {ticket_id}")
        raise typer.Exit(1)

    t, d = trail["ticket"], trail.get("decision")
    console.print(
        Panel(
            f"[bold]{t['subject_raw']}[/]\n"
            f"from {t['from_name']} <{t['from_email']}>  ·  {t['received_at']}\n\n"
            f"{(t['body_raw'] or '')[:600]}",
            title=ticket_id,
        )
    )

    if t["subject_norm"] != t["subject_raw"] or t["body_norm"] != t["body_raw"]:
        console.print(
            Panel(
                f"repairs: {t['repairs']}   quality: {t['text_quality']:.2f}   "
                f"lang: {t['language']}\n\n{(t['body_norm'] or '')[:600]}",
                title="normalized",
                border_style="cyan",
            )
        )

    if t["injection_suspected"]:
        spans = t["injection_spans"] or []
        console.print(
            Panel(
                "\n".join(f"[red]{s['pattern']}[/]: {s['text'][:90]}" for s in spans),
                title="INJECTION DETECTED — quarantined, never re-prompted",
                border_style="red",
            )
        )

    steps = Table("seq", "agent", "model", "ms", "try", "error", "output", box=None)
    for s in trail.get("steps", []):
        out = json.dumps(s["parsed_output"])[:70] if s["parsed_output"] else ""
        steps.add_row(
            str(s["seq"]), s["agent"], (s["model_id"] or "")[-14:],
            str(s["latency_ms"] or ""), str(s["attempt"]),
            f"[red]{s['error_type']}[/]" if s["error_type"] else "",
            out,
        )
    console.print(Panel(steps, title="agent steps", border_style="blue"))

    if trail.get("policy_events"):
        pol = Table("rule", "triggered by", "before", "→", "after", box=None)
        for e in trail["policy_events"]:
            pol.add_row(
                f"[red]{e['rule_id']}[/]", (e["triggered_by"] or "")[:44],
                e["route_before"], "→", f"[bold]{e['route_after']}[/]",
            )
        console.print(Panel(pol, title="policy events", border_style="red"))

    if d:
        console.print(
            Panel(
                f"margin={d['c_margin']}  crossmodel={d['c_crossmodel']}  "
                f"selfreport={d['c_selfreport']}\n"
                f"penalties={d['penalties']}\n"
                f"[bold]composite={d['composite']}[/]  "
                f"(tau_auto={d['tau_auto']} tau_draft={d['tau_draft']})",
                title="confidence",
                border_style="magenta",
            )
        )
        route = d["final_route"]
        console.print(
            Panel(
                f"[{ROUTE_STYLE[route]}][bold]{route.upper()}[/][/]"
                f"   queue={d['escalation_queue'] or '-'}   pri={d['priority']}\n\n"
                f"[bold]why not auto-resolved:[/] {d['binding_constraint']}\n\n"
                f"contributors: {d['contributors']}",
                title="final decision",
                border_style=ROUTE_STYLE[route],
            )
        )

    if trail.get("draft") and trail["draft"]["body"]:
        console.print(
            Panel(trail["draft"]["body"], title="draft reply", border_style="green")
        )

    for r in trail.get("reviews", []):
        console.print(
            f"[bold]{r['action']}[/] by {r['reviewer']} at {r['acted_at']}"
            + (f" — {r['reject_reason']}" if r["reject_reason"] else "")
        )


def _latest_run(ticket_id: str) -> dict:
    trail = Repository().trail(ticket_id)
    if not trail or not trail.get("run"):
        console.print(f"[red]no run found for[/] {ticket_id}")
        raise typer.Exit(1)
    return trail


@app.command()
def approve(ticket_id: str, as_: str = typer.Option(..., "--as", help="reviewer")) -> None:
    """Approve a drafted reply as-is."""
    trail = _latest_run(ticket_id)
    body = (trail.get("draft") or {}).get("body")
    Repository().record_review(
        ticket_id, str(trail["run"]["id"]), as_, "approve",
        draft_before=body, draft_after=body,
    )
    console.print(f"[green]approved[/] {ticket_id} (would send {len(body or '')} chars)")


@app.command()
def edit(
    ticket_id: str,
    file: Path = typer.Option(..., help="File containing the edited reply"),
    as_: str = typer.Option(..., "--as", help="reviewer"),
) -> None:
    """Approve a drafted reply with edits."""
    trail = _latest_run(ticket_id)
    before = (trail.get("draft") or {}).get("body")
    after = file.read_text(encoding="utf-8")
    Repository().record_review(
        ticket_id, str(trail["run"]["id"]), as_, "edit",
        draft_before=before, draft_after=after,
    )
    console.print(f"[yellow]edited[/] {ticket_id}")


@app.command()
def reject(
    ticket_id: str,
    reason: str = typer.Option(..., help="Why the draft was rejected"),
    as_: str = typer.Option(..., "--as", help="reviewer"),
) -> None:
    """Reject a drafted reply. This is the system's best quality signal."""
    trail = _latest_run(ticket_id)
    Repository().record_review(
        ticket_id, str(trail["run"]["id"]), as_, "reject",
        draft_before=(trail.get("draft") or {}).get("body"), reject_reason=reason,
    )
    console.print(f"[red]rejected[/] {ticket_id}: {reason}")


@app.command()
def stats() -> None:
    """Route distribution, rule fires, override rate, failures."""
    repo = Repository()
    console.print(repo.counts())

    for title, rows in (
        ("route distribution", repo.route_distribution()),
        ("policy rule fires", repo.rule_fire_counts()),
        ("human override rate", repo.override_rate()),
        ("agent failures", repo.failure_counts()),
    ):
        if not rows:
            continue
        table = Table(title=title, box=None)
        for col in rows[0]:
            table.add_column(col)
        for r in rows:
            table.add_row(*[str(v) for v in r.values()])
        console.print(table)


if __name__ == "__main__":
    app()
