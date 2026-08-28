"""Which Gemini models actually return avg_logprobs?

The confidence design (ADR-003) wants a token-probability signal. flash-lite rejects it.
Find out what does, so we can decide: switch the classifier model, or drop to 3 signals.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


class Out(BaseModel):
    label: str


PROMPT = "Classify in one word: 'My invoice is $12 higher than usual, why?'"

CANDIDATES = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]


def probe(client: genai.Client, model: str) -> str:
    base = {
        "response_mime_type": "application/json",
        "response_schema": Out,
        "temperature": 0.0,
    }
    # does it accept the flag at all?
    try:
        r = client.models.generate_content(
            model=model, contents=PROMPT, config={**base, "response_logprobs": True}
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("\n", " ")
        if "not enabled" in msg or "INVALID_ARGUMENT" in msg:
            return "no  - rejects response_logprobs"
        if "NOT_FOUND" in msg or "404" in msg:
            return "n/a - model not available"
        return f"err - {type(e).__name__}: {msg[:70]}"

    cand = r.candidates[0]
    avg = getattr(cand, "avg_logprobs", None)
    if avg is None:
        return "no  - accepted flag but avg_logprobs is None"
    return f"YES - avg={avg:.4f} exp={math.exp(avg):.4f}"


def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    print(f"{'model':<26} logprobs")
    print("-" * 70)
    for m in CANDIDATES:
        print(f"{m:<26} {probe(client, m)}")


if __name__ == "__main__":
    main()
