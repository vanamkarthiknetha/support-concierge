"""Phase 0 gate: verify the three things the architecture depends on.

1. Gemini reachable, and which of our pinned model IDs actually exist.
2. Schema-constrained structured output works.
3. `avg_logprobs` comes back  <- the whole confidence design (ADR-003) rests on this.

Run:  uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

CHEAP = os.getenv("MODEL_CHEAP", "gemini-3.1-flash-lite")
SMART = os.getenv("MODEL_SMART", "gemini-3.7-flash")

TICKET = (
    "Subject: This is the third time I've been overcharged\n"
    "I am extremely frustrated. This is the THIRD month in a row you've charged me "
    "for the Pro plan when I downgraded to Basic in March. If this isn't fixed today "
    "I'm cancelling and telling my whole team to move to a competitor."
)


class Classification(BaseModel):
    labels: list[str] = Field(description="intent labels that apply")
    confidence: float = Field(description="0-1 confidence in the labels")


def ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def main() -> int:
    failures: list[str] = []

    # ---- 1. key present -------------------------------------------------
    print("\n[1] API key")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        fail("no GEMINI_API_KEY / GOOGLE_API_KEY in .env")
        return 1
    ok(f"key present ({key[:8]}...{key[-4:]})")

    # ---- 2. which models exist -----------------------------------------
    print("\n[2] Model availability")
    from google import genai

    client = genai.Client(api_key=key)
    try:
        available = {m.name.removeprefix("models/") for m in client.models.list()}
        ok(f"models.list() returned {len(available)} models")
    except Exception as e:  # noqa: BLE001
        fail(f"models.list() failed: {type(e).__name__}: {e}")
        available = set()

    resolved: dict[str, str] = {}
    for label, want in (("cheap", CHEAP), ("smart", SMART)):
        if want in available:
            ok(f"{label}: {want} available")
            resolved[label] = want
        else:
            near = sorted(m for m in available if "flash" in m and "preview" not in m)
            fail(f"{label}: {want} NOT available")
            print(f"         flash models actually available: {near[:12]}")
            resolved[label] = near[0] if near else want

    # ---- 3. structured output ------------------------------------------
    print("\n[3] Structured output (schema-constrained JSON)")
    model = resolved["cheap"]
    try:
        resp = client.models.generate_content(
            model=model,
            contents=f"Classify this support ticket.\n\n{TICKET}",
            config={
                "response_mime_type": "application/json",
                "response_schema": Classification,
                "temperature": 0.0,
            },
        )
        parsed = resp.parsed
        ok(f"parsed -> {parsed!r}")
    except Exception as e:  # noqa: BLE001
        fail(f"structured output failed: {type(e).__name__}: {e}")
        failures.append("structured_output")
        resp = None

    # ---- 4. logprobs  <- THE GATE --------------------------------------
    print("\n[4] Logprobs (avg_logprobs) -- gates the confidence design")
    try:
        resp2 = client.models.generate_content(
            model=model,
            contents=f"Classify this support ticket.\n\n{TICKET}",
            config={
                "response_mime_type": "application/json",
                "response_schema": Classification,
                "temperature": 0.0,
                "response_logprobs": True,
            },
        )
        cand = resp2.candidates[0]
        avg = getattr(cand, "avg_logprobs", None)
        if avg is None:
            fail("response_logprobs accepted but avg_logprobs is None")
            failures.append("logprobs")
        else:
            import math

            ok(f"avg_logprobs = {avg:.4f}  ->  exp() = {math.exp(avg):.4f}")
    except Exception as e:  # noqa: BLE001
        fail(f"logprobs failed: {type(e).__name__}: {e}")
        failures.append("logprobs")

    # ---- 5. LangChain binding (what LangGraph nodes will use) ----------
    print("\n[5] LangChain binding + with_structured_output")
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(model=resolved["cheap"], temperature=0.0)
        out = llm.with_structured_output(Classification).invoke(
            f"Classify this support ticket.\n\n{TICKET}"
        )
        ok(f"with_structured_output -> {out!r}")
    except Exception as e:  # noqa: BLE001
        fail(f"langchain binding failed: {type(e).__name__}: {e}")
        failures.append("langchain")

    # ---- 6. Postgres ----------------------------------------------------
    print("\n[6] Postgres")
    try:
        import psycopg

        dsn = os.environ["DATABASE_URL"]
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            v = conn.execute("select version()").fetchone()[0]
        ok(v.split(",")[0])
    except Exception as e:  # noqa: BLE001
        fail(f"postgres failed: {type(e).__name__}: {e}")
        failures.append("postgres")

    # ---- verdict --------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"resolved cheap = {resolved['cheap']}")
    print(f"resolved smart = {resolved['smart']}")
    if failures:
        print(f"FAILURES: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
