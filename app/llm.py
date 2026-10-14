"""Thin wrapper over the Anthropic SDK: shared client, pricing, structured calls with a cached profile prefix."""
from __future__ import annotations

from typing import TypeVar

import anthropic
from pydantic import BaseModel

from app.config import load_config

T = TypeVar("T", bound=BaseModel)

# $ per 1M tokens (input, output). Cache reads are ~10% of input.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def model(role: str) -> str:
    defaults = {"categorize": "claude-sonnet-5", "agent": "claude-opus-5-5"}
    return (load_config().get("models") or {}).get(role, defaults[role])


def cost(model_id: str, usage) -> float:
    inp, out = PRICES.get(model_id, (5.0, 25.0))
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        usage.input_tokens * inp
        + cache_read * inp * 0.1
        + cache_write * inp * 1.25
        + usage.output_tokens * out
    ) / 1_000_000


class Refused(RuntimeError):
    pass


async def structured(
    *,
    role: str,
    cached_context: str,
    instructions: str,
    user: str,
    output: type[T],
    effort: str = "medium",
    max_tokens: int = 16000,
) -> tuple[T, float]:
    """One structured-output call. `cached_context` (profile, preferences) is a stable,
    cache-marked prefix so repeated scoring/tailoring calls reuse it."""
    m = model(role)
    resp = await client().messages.parse(
        model=m,
        max_tokens=max_tokens,
        system=[
            {"type": "text", "text": instructions},
            {"type": "text", "text": cached_context, "cache_control": {"type": "ephemeral"}},
        ],
        output_config={"effort": effort},
        messages=[{"role": "user", "content": user}],
        output_format=output,
    )
    if resp.stop_reason == "refusal":
        raise Refused(getattr(resp.stop_details, "explanation", "") or "model declined")
    if resp.parsed_output is None:
        raise RuntimeError(f"no structured output (stop_reason={resp.stop_reason})")
    return resp.parsed_output, cost(m, resp.usage)
