"""LLM client: rate limiting, retries, schema repair, fault injection, circuit breaker.

Every agent call goes through here. Agents return an AgentOutcome and never raise
into the graph -- a provider failure is data the router acts on, not an exception
that unwinds the pipeline and drops a ticket.

The rate limiter is not a nicety: the free tier was measured at ~15 RPM
(429 after 13 calls in 21s), and one full run is ~72 calls.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, ValidationError

from concierge.config import get_settings
from concierge.models import AgentStep, FailureType
from concierge.llm.faults import FaultInjector, InjectedFault

T = TypeVar("T", bound=BaseModel)


@dataclass
class AgentOutcome(Generic[T]):
    """Result of an agent call. Never raises; the caller inspects `ok`."""

    value: T | None = None
    ok: bool = False
    error_type: FailureType | None = None
    error_detail: str = ""
    attempts: int = 0
    latency_ms: int = 0
    steps: list[AgentStep] = field(default_factory=list)
    raw: str | None = None


class RateLimiter:
    """Token bucket over a sliding window. Thread-safe.

    Blocks rather than failing: a triage run that takes 6 minutes is fine; one
    that 429s halfway through and escalates everything is not.
    """

    def __init__(self, rpm: int):
        self.rpm = max(1, rpm)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 60.0:
                self._calls.popleft()
            if len(self._calls) < self.rpm:
                self._calls.append(now)
                return 0.0
            wait = 60.0 - (now - self._calls[0]) + 0.1
        time.sleep(max(0.0, wait))
        return self.acquire() + wait


class CircuitBreaker:
    """Trips when the provider is failing broadly.

    During a brownout a system making low-quality automated decisions is worse
    than one making none: when open, every ticket escalates without an LLM call.
    Failing loudly into the human queue gives ops a signal instead of a slow-
    burning quality regression.
    """

    def __init__(self, error_rate: float, window: int, cooldown_s: float):
        self.error_rate = error_rate
        self.window = window
        self.cooldown_s = cooldown_s
        self._recent: deque[bool] = deque(maxlen=window)
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at > self.cooldown_s:
                self._opened_at = None      # half-open: allow a probe through
                self._recent.clear()
                return False
            return True

    def record(self, ok: bool) -> None:
        with self._lock:
            self._recent.append(ok)
            if len(self._recent) < self.window:
                return
            failures = sum(1 for r in self._recent if not r)
            if failures / len(self._recent) >= self.error_rate:
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._recent.clear()
            self._opened_at = None


def prompt_hash(system: str, user: str) -> str:
    """Ties an output to the exact prompt version that produced it."""
    return hashlib.sha256(f"{system}\x00{user}".encode()).hexdigest()[:12]


class LLMClient:
    """Wraps the Gemini SDK with everything production needs and demos skip."""

    def __init__(
        self,
        api_key: str | None = None,
        faults: FaultInjector | None = None,
        rate_limiter: RateLimiter | None = None,
    ):
        s = get_settings()
        self.settings = s
        self._client = genai.Client(api_key=api_key or s.gemini_api_key)
        self.faults = faults or FaultInjector()
        # Quotas and health are PER MODEL. Exhausting the smart model's quota must
        # not stop the cheap model from working -- a shared bucket turned one
        # model's rate limit into a total outage on the first live run.
        self._shared_limiter = rate_limiter
        self._limiters: dict[str, RateLimiter] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    def _limiter_for(self, model: str) -> RateLimiter:
        if self._shared_limiter is not None:
            return self._shared_limiter
        if model not in self._limiters:
            self._limiters[model] = RateLimiter(self.settings.llm_rate_limit_rpm)
        return self._limiters[model]

    def _breaker_for(self, model: str) -> CircuitBreaker:
        s = self.settings
        if model not in self._breakers:
            self._breakers[model] = CircuitBreaker(
                s.breaker_error_rate, s.breaker_window, s.breaker_cooldown_s
            )
        return self._breakers[model]

    def call(
        self,
        *,
        agent: str,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        seq: int,
        temperature: float = 0.0,
        fallback_model: str | None = None,
    ) -> AgentOutcome[T]:
        """One schema-constrained call, with retry, repair, and model fallback.

        Returns an outcome. Never raises.

        Model fallback exists because quota exhaustion on the premium model should
        cost MODEL QUALITY, not automation rate. On the first live run the smart
        model's free-tier quota ran out, every decision call 429'd, and every clean
        ticket degraded to draft_for_review -- automation dropped to zero for a
        reason that had nothing to do with the tickets. Falling back to the cheap
        model keeps the pipeline working; the deterministic safety layer is
        unaffected either way, which is precisely why a weaker model here is
        tolerable.
        """
        steps: list[AgentStep] = []
        ph = prompt_hash(system, user)
        started = time.monotonic()

        breaker = self._breaker_for(model)
        limiter = self._limiter_for(model)

        if breaker.is_open:
            return AgentOutcome(
                ok=False,
                error_type=FailureType.CIRCUIT_OPEN,
                error_detail="circuit breaker open; provider failing broadly",
                attempts=0,
                steps=[
                    AgentStep(
                        seq=seq, agent=agent, model_id=model, prompt_hash=ph,
                        attempt=1, error_type=FailureType.CIRCUIT_OPEN,
                        error_detail="circuit breaker open",
                    )
                ],
            )

        max_attempts = self.settings.llm_max_retries + 1
        contents = user

        for attempt in range(1, max_attempts + 1):
            t0 = time.monotonic()
            raw: str | None = None
            try:
                fault = self.faults.check(agent)
                if fault is not None:
                    raise fault

                limiter.acquire()
                resp = self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config={
                        "system_instruction": system,
                        "response_mime_type": "application/json",
                        "response_schema": schema,
                        "temperature": temperature,
                    },
                )
                raw = resp.text
                parsed = resp.parsed
                if parsed is None:
                    parsed = schema.model_validate_json(raw or "")

                latency = int((time.monotonic() - t0) * 1000)
                steps.append(
                    AgentStep(
                        seq=seq, agent=agent, model_id=model, prompt_hash=ph,
                        input={"user_chars": len(user)},
                        raw_output=(raw or "")[:4000],
                        parsed_output=json.loads(parsed.model_dump_json()),
                        latency_ms=latency,
                        input_tokens=_usage(resp, "prompt_token_count"),
                        output_tokens=_usage(resp, "candidates_token_count"),
                        attempt=attempt,
                    )
                )
                breaker.record(True)
                return AgentOutcome(
                    value=parsed, ok=True, attempts=attempt,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    steps=steps, raw=raw,
                )

            except Exception as exc:  # noqa: BLE001 - classified below, never propagated
                etype, detail, retryable = _classify(exc)
                steps.append(
                    AgentStep(
                        seq=seq, agent=agent, model_id=model, prompt_hash=ph,
                        input={"user_chars": len(user)},
                        raw_output=(raw or "")[:4000] if raw else None,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        attempt=attempt, error_type=etype, error_detail=detail[:500],
                    )
                )
                # A 429 means "slow down", not "the provider is unhealthy".
                # Only genuine failures count toward tripping the breaker.
                if etype is not FailureType.RATE_LIMITED:
                    breaker.record(False)

                if attempt >= max_attempts or not retryable:
                    # Quota exhausted on this model? Retry once on the cheaper one
                    # before giving up. Losing model quality beats losing the
                    # ticket to an unnecessary escalation.
                    if (
                        etype is FailureType.RATE_LIMITED
                        and fallback_model
                        and fallback_model != model
                    ):
                        fb = self.call(
                            agent=agent, model=fallback_model, system=system,
                            user=user, schema=schema, seq=seq,
                            temperature=temperature, fallback_model=None,
                        )
                        fb.steps = steps + fb.steps
                        if fb.ok:
                            fb.error_detail = (
                                f"{model} quota exhausted; served by {fallback_model}"
                            )
                        return fb

                    return AgentOutcome(
                        ok=False, error_type=etype, error_detail=detail,
                        attempts=attempt,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        steps=steps, raw=raw,
                    )

                # One repair attempt for malformed output: re-prompt with the
                # validation error. Never regex-scrape JSON out of prose, and never
                # eval() -- a truncated object is not a partial success.
                if etype is FailureType.MALFORMED:
                    contents = (
                        f"{user}\n\n---\nYour previous response failed schema validation:\n"
                        f"{detail[:300]}\nReturn ONLY valid JSON matching the schema."
                    )

                backoff = min(2.0**attempt, 8.0) + random.uniform(0, 0.5)
                if etype is FailureType.RATE_LIMITED:
                    backoff = max(backoff, 20.0)
                time.sleep(backoff)

        return AgentOutcome(
            ok=False, error_type=FailureType.PROVIDER_ERROR,
            error_detail="exhausted retries", attempts=max_attempts, steps=steps,
        )


def _usage(resp: Any, field_name: str) -> int | None:
    meta = getattr(resp, "usage_metadata", None)
    return getattr(meta, field_name, None) if meta else None


def _classify(exc: BaseException) -> tuple[FailureType, str, bool]:
    """Map an exception to (type, detail, retryable).

    A chain, not one broad class: the distinction between retryable (429, 5xx,
    timeout) and non-retryable (400, schema mismatch) is the whole point.
    """
    if isinstance(exc, InjectedFault):
        return exc.failure_type, f"injected fault: {exc.failure_type.value}", exc.retryable
    if isinstance(exc, ValidationError):
        return FailureType.MALFORMED, str(exc), True
    if isinstance(exc, json.JSONDecodeError):
        return FailureType.MALFORMED, str(exc), True
    if isinstance(exc, genai_errors.ClientError):
        code = getattr(exc, "code", None)
        if code == 429 or "RESOURCE_EXHAUSTED" in str(exc):
            return FailureType.RATE_LIMITED, str(exc)[:300], True
        return FailureType.PROVIDER_ERROR, str(exc)[:300], False
    if isinstance(exc, genai_errors.ServerError):
        return FailureType.PROVIDER_ERROR, str(exc)[:300], True
    if isinstance(exc, TimeoutError):
        return FailureType.TIMEOUT, str(exc), True

    text = str(exc).lower()
    if "timeout" in text or "deadline" in text:
        return FailureType.TIMEOUT, str(exc)[:300], True
    if "429" in text or "quota" in text or "exhausted" in text:
        return FailureType.RATE_LIMITED, str(exc)[:300], True
    if "safety" in text or "blocked" in text or "refus" in text:
        return FailureType.REFUSED, str(exc)[:300], False
    return FailureType.PROVIDER_ERROR, f"{type(exc).__name__}: {exc}"[:300], True
