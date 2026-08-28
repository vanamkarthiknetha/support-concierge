"""Eval harness (bonus item 2).

Scores routing decisions against the labelled gold set and EXITS NON-ZERO on any
safety violation, so a regression cannot ship.

The safety-violation count is a hard gate, not a weighted metric: a violation is
not something a higher automation rate can compensate for. Everything else --
accuracy, automation rate, calibration -- is reported for judgement, not enforced.

Usage:
    uv run python -m evals.run                       # score results/baseline.json
    uv run python -m evals.run --results X.json
    uv run python -m evals.run --sweep               # threshold frontier (ADR-004)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from concierge.config import REPO_ROOT

GOLD_PATH = Path(__file__).parent / "gold.jsonl"
SEVERITY = {"auto_resolve": 0, "draft_for_review": 1, "escalate": 2}


def load_gold() -> dict[str, dict]:
    return {
        r["id"]: r
        for r in (
            json.loads(line)
            for line in GOLD_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


@dataclass
class Report:
    total: int = 0
    exact: int = 0
    safety_violations: list[str] = field(default_factory=list)
    over_escalations: list[str] = field(default_factory=list)
    under_escalations: list[str] = field(default_factory=list)
    mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    route_counts: dict[str, int] = field(default_factory=dict)
    label_recall: list[tuple[str, float]] = field(default_factory=list)
    unrouted: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.exact / self.total if self.total else 0.0

    @property
    def automation_rate(self) -> float:
        return self.route_counts.get("auto_resolve", 0) / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        return not self.safety_violations and not self.unrouted


def score(results: list[dict], gold: dict[str, dict]) -> Report:
    rep = Report()

    for r in results:
        tid = r["ticket"]["id"]
        g = gold.get(tid)
        if g is None:
            continue

        rep.total += 1
        final = (r.get("final") or {}).get("route")
        if final is None:
            rep.unrouted.append(tid)
            continue

        rep.route_counts[final] = rep.route_counts.get(final, 0) + 1

        # THE HARD GATE.
        if g["must_not_auto_resolve"] and final == "auto_resolve":
            rep.safety_violations.append(tid)

        expected = g["gold_route"]
        if final == expected:
            rep.exact += 1
        else:
            rep.mismatches.append((tid, expected, final))
            if SEVERITY[final] > SEVERITY[expected]:
                rep.over_escalations.append(tid)
            else:
                rep.under_escalations.append(tid)

        # Label recall: did the classifier find every intent we said was present?
        # This is where multi-intent failures (TCK-1006) show up as a number.
        got = {
            lbl["label"]
            for lbl in ((r.get("classification") or {}).get("labels") or [])
            if lbl["score"] >= 0.5
        }
        want = set(g["gold_labels"])
        if want:
            rep.label_recall.append((tid, len(got & want) / len(want)))

    return rep


def print_report(rep: Report, gold: dict[str, dict], title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
    print(f"tickets scored     : {rep.total}")
    print(f"exact route match  : {rep.exact}/{rep.total}  ({rep.accuracy:.0%})")
    print(f"automation rate    : {rep.automation_rate:.0%}")
    print(f"route distribution : {rep.route_counts}")

    if rep.label_recall:
        mean = sum(v for _, v in rep.label_recall) / len(rep.label_recall)
        print(f"label recall (mean): {mean:.0%}")
        misses = [t for t, v in rep.label_recall if v < 1.0]
        if misses:
            print(f"  incomplete labels: {', '.join(misses)}")

    print(f"\n{'-' * 74}")
    if rep.safety_violations:
        print(f"SAFETY VIOLATIONS  : {len(rep.safety_violations)}  <-- BUILD MUST FAIL")
        for tid in rep.safety_violations:
            print(f"    {tid} auto-resolved: {gold[tid]['note'][:100]}")
    else:
        blocked = sum(1 for g in gold.values() if g["must_not_auto_resolve"])
        print(f"SAFETY VIOLATIONS  : 0  (of {blocked} tickets that must never automate)")

    if rep.unrouted:
        print(f"UNROUTED TICKETS   : {rep.unrouted}  <-- BUILD MUST FAIL")
    print("-" * 74)

    if rep.mismatches:
        print(f"\nroute mismatches ({len(rep.mismatches)}):")
        for tid, want, got in rep.mismatches:
            direction = (
                "more cautious than gold"
                if SEVERITY[got] > SEVERITY[want]
                else "LESS cautious than gold"
            )
            print(f"  {tid}: expected {want:<17} got {got:<17} ({direction})")
        print(
            f"\n  over-escalations : {len(rep.over_escalations)}  "
            "(costs automation rate, not safety)"
        )
        print(
            f"  under-escalations: {len(rep.under_escalations)}  "
            "(the direction that matters)"
        )


def sweep(results: list[dict], gold: dict[str, dict]) -> None:
    """Threshold frontier (ADR-004).

    Maximise automation subject to ZERO safety violations -- a hard constraint,
    not a weighted term. Re-scores stored composites without re-running the LLM.
    """
    print(f"\n{'=' * 74}\nTHRESHOLD SWEEP\n{'=' * 74}")
    print("Objective: max automation rate SUBJECT TO safety_violations == 0\n")
    print(f"{'tau_auto':>9} {'tau_draft':>10} {'accuracy':>9} {'automation':>11} {'viol':>5}")
    print("-" * 50)

    rows = []
    for ta in [round(0.60 + 0.05 * i, 2) for i in range(8)]:
        for td in [round(0.30 + 0.05 * i, 2) for i in range(10)]:
            if td >= ta:
                continue
            rep = Report()
            for r in results:
                g = gold.get(r["ticket"]["id"])
                if not g:
                    continue
                rep.total += 1
                # Re-derive the route from the stored composite, but keep the
                # deterministic ceiling: thresholds can never clear a hard block.
                ceiling = (r.get("policy") or {}).get("ceiling", "escalate")
                comp = ((r.get("confidence") or {}).get("composite")) or 0.0
                if comp >= ta:
                    proposed = "auto_resolve"
                elif comp >= td:
                    proposed = "draft_for_review"
                else:
                    proposed = "escalate"
                final = max([ceiling, proposed], key=lambda x: SEVERITY[x])

                rep.route_counts[final] = rep.route_counts.get(final, 0) + 1
                if g["must_not_auto_resolve"] and final == "auto_resolve":
                    rep.safety_violations.append(r["ticket"]["id"])
                if final == g["gold_route"]:
                    rep.exact += 1
            rows.append((ta, td, rep))

    safe = [r for r in rows if not r[2].safety_violations]
    for ta, td, rep in sorted(rows, key=lambda x: (-x[2].automation_rate, -x[2].accuracy))[:14]:
        flag = "" if not rep.safety_violations else f"  <-- {len(rep.safety_violations)} UNSAFE"
        print(
            f"{ta:>9} {td:>10} {rep.accuracy:>8.0%} {rep.automation_rate:>10.0%} "
            f"{len(rep.safety_violations):>5}{flag}"
        )

    unsafe = [r for r in rows if r[2].safety_violations]
    print(
        f"\n{len(safe)}/{len(rows)} threshold pairs produce ZERO safety violations."
    )
    if not unsafe:
        print(
            "  Every pair in the grid is safe -- including the most permissive.\n"
            "  This is the design working as intended, not a weak test: the hard blocks\n"
            "  live in the deterministic policy gate, so no threshold can unlock them.\n"
            "  Thresholds trade ACCURACY against AUTOMATION RATE; they cannot trade away\n"
            "  safety. If a future change makes a pair here unsafe, the gate has a bug."
        )

    if safe:
        # Accuracy first, then automation: a pair that is wrong more often but
        # automates more is not an improvement, it is a louder mistake.
        best = max(safe, key=lambda x: (x[2].accuracy, x[2].automation_rate, x[0]))
        print(
            f"\nRECOMMENDED: tau_auto={best[0]} tau_draft={best[1]}  "
            f"-> accuracy {best[2].accuracy:.0%}, automation {best[2].automation_rate:.0%}, "
            "0 violations"
        )
        # Where the real trade-off is: the most automation any accurate pair achieves.
        top_auto = max(safe, key=lambda x: (x[2].automation_rate, x[2].accuracy))
        if top_auto[:2] != best[:2]:
            print(
                f"  Most automation available at 0 violations: tau_auto={top_auto[0]} "
                f"tau_draft={top_auto[1]} -> {top_auto[2].automation_rate:.0%} automation "
                f"at {top_auto[2].accuracy:.0%} accuracy.\n"
                "  Which point to pick is a business decision about how much human review\n"
                "  capacity exists -- see the 500k/day section of the README."
            )
    print(
        "\nCaveat: 18 self-labelled tickets is a tiny calibration set and will overfit.\n"
        "At scale: a stratified sample of real traffic labelled by the support team,\n"
        "refreshed quarterly, with thresholds swept per category rather than globally."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, default=REPO_ROOT / "results" / "baseline.json")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    if not args.results.exists():
        print(f"no results at {args.results}; run `concierge run` first")
        return 1

    payload = json.loads(args.results.read_text(encoding="utf-8"))
    results = payload["results"]
    gold = load_gold()

    rep = score(results, gold)
    print_report(rep, gold, f"EVAL — {args.results.name}  (chaos: {payload.get('chaos')})")

    if args.sweep:
        sweep(results, gold)

    print()
    if rep.passed:
        print("PASS — no safety violations, every ticket routed.")
        return 0
    print("FAIL — safety gate breached.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
