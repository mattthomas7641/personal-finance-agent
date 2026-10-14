"""Thin wrapper over the Anthropic SDK: shared client, pricing, structured calls with a cached profile prefix."""
from __future__ import annotations

from typing import TypeVar

import anthropic
from pydantic import BaseModel

from app.config import load_config

T = TypeVar("T", bound=BaseModel)

# $ per 1M tokens: (input, output, cache-read share of input). Cache writes are 1.25x input (5-minute TTL).
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.1),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-opus-5": (5.0, 25.0, 0.1),
    "claude-sonnet-5-5": (2.0, 10.0, 0.1),
    "claude-sonnet-5": (2.0, 10.0, 0.1),
    "claude-haiku-5-5": (0.10, 0.50, 0.1),
    "claude-haiku-4-5": (1.0, 5.0, 0.1),
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
    inp, out, read_share = PRICES.get(model_id, (5.0, 25.0, 0.1))
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        usage.input_tokens * inp
        + cache_read * inp * read_share
        + cache_write * inp * 1.25
        + usage.output_tokens * out
    ) / 1_000_000


def friendly_api_error(e: Exception) -> str:
    """Turn an Anthropic SDK error into something the user can act on."""
    if isinstance(e, anthropic.AuthenticationError):
        return "The Claude API key was rejected. Check ANTHROPIC_API_KEY in .env, then restart Northstar."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "The Claude API key isn't allowed to use this model."
    if isinstance(e, anthropic.RateLimitError):
        return "Claude is rate-limiting requests right now. Try again in a minute."
    if isinstance(e, anthropic.BadRequestError) and "credit" in str(e).lower():
        return "Your Anthropic account is out of credits. Add credits at console.anthropic.com."
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return "Couldn't reach the Claude API. Check your internet connection."
    if isinstance(e, anthropic.InternalServerError) or getattr(e, "status_code", 0) in (529,):
        return "Claude is temporarily unavailable. Try again shortly."
    return f"The Claude API returned an error ({type(e).__name__})."


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
