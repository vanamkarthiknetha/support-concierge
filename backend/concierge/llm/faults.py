"""Fault injection for the chaos harness.

Requirement 5 says *show* us what happens when an agent fails. So we demonstrate
it rather than asserting it: `--chaos agent=classifier,mode=timeout,rate=1.0`
forces a named agent to fail in a named way, and the results file shows the
pipeline degrading toward escalation instead of crashing or dropping the ticket.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from concierge.models import FailureType


class InjectedFault(Exception):
    """A deliberately injected failure. Carries how it should be classified."""

    def __init__(self, failure_type: FailureType, retryable: bool = True):
        super().__init__(f"injected {failure_type.value}")
        self.failure_type = failure_type
        self.retryable = retryable


MODES: dict[str, tuple[FailureType, bool]] = {
    "timeout": (FailureType.TIMEOUT, True),
    "malformed": (FailureType.MALFORMED, True),
    "error_500": (FailureType.PROVIDER_ERROR, True),
    "rate_limited": (FailureType.RATE_LIMITED, True),
    "refusal": (FailureType.REFUSED, False),
}


@dataclass
class FaultSpec:
    agent: str          # agent name, or "*" for every agent
    mode: str
    rate: float = 1.0

    def matches(self, agent: str) -> bool:
        return self.agent in ("*", agent)


class FaultInjector:
    """Off by default. Deterministic when seeded, so chaos runs are reproducible --
    an audit trail that can't be reproduced isn't much of an audit trail."""

    def __init__(self, specs: list[FaultSpec] | None = None, seed: int | None = 1337):
        self.specs = specs or []
        self._rng = random.Random(seed)

    @property
    def enabled(self) -> bool:
        return bool(self.specs)

    def check(self, agent: str) -> InjectedFault | None:
        for spec in self.specs:
            if not spec.matches(agent):
                continue
            if self._rng.random() > spec.rate:
                continue
            ftype, retryable = MODES.get(spec.mode, (FailureType.PROVIDER_ERROR, True))
            return InjectedFault(ftype, retryable)
        return None

    @classmethod
    def parse(cls, spec_strings: list[str] | None, seed: int | None = 1337) -> "FaultInjector":
        """Parse `agent=classifier,mode=timeout,rate=1.0` (repeatable)."""
        specs: list[FaultSpec] = []
        for raw in spec_strings or []:
            parts = dict(
                kv.split("=", 1) for kv in raw.split(",") if "=" in kv
            )
            mode = parts.get("mode", "error_500")
            if mode not in MODES:
                raise ValueError(f"unknown chaos mode {mode!r}; choose from {sorted(MODES)}")
            specs.append(
                FaultSpec(
                    agent=parts.get("agent", "*"),
                    mode=mode,
                    rate=float(parts.get("rate", 1.0)),
                )
            )
        return cls(specs, seed=seed)

    def describe(self) -> str:
        if not self.specs:
            return "none"
        return "; ".join(f"{s.agent}:{s.mode}@{s.rate}" for s in self.specs)
