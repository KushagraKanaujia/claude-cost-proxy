"""Anthropic list prices in USD per million tokens.

These are estimates for local cost tracking, not billing data. Check
https://www.anthropic.com/pricing for current rates. Model IDs are matched by
prefix, so dated snapshots (e.g. ``claude-sonnet-4-5-20250929``) resolve to
their family entry. Cache writes are priced at the 5-minute TTL rate
(1.25x input); cache reads use each model's published read rate.
"""
from typing import Dict, Mapping

Prices = Dict[str, float]


def _p(inp: float, out: float, cache_read: float = None) -> Prices:
    return {
        "input": inp,
        "output": out,
        "cache_write": round(inp * 1.25, 4),
        "cache_read": cache_read if cache_read is not None else round(inp * 0.1, 4),
    }


# Longest prefix wins, so more specific IDs can sit beside family defaults.
PRICING: Dict[str, Prices] = {
    # Current generation
    "claude-fable-5-1": _p(10.00, 50.00, cache_read=0.25),
    "claude-mythos-5-1": _p(10.00, 50.00, cache_read=0.25),
    "claude-fable-5": _p(10.00, 50.00),
    "claude-mythos-5": _p(10.00, 50.00),
    "claude-opus-5-5": _p(4.00, 20.00, cache_read=0.20),
    "claude-opus-5": _p(5.00, 25.00),
    "claude-sonnet-5-5": _p(2.00, 10.00, cache_read=0.20),
    "claude-sonnet-5": _p(2.00, 10.00),
    # Haiku 5.5 is $0.10/$0.50 for prompts up to 100K tokens (higher above).
    "claude-haiku-5-5": _p(0.10, 0.50),
    # Claude 4.x
    "claude-opus-4-8": _p(5.00, 25.00),
    "claude-opus-4-7": _p(5.00, 25.00),
    "claude-opus-4-6": _p(5.00, 25.00),
    "claude-opus-4-5": _p(5.00, 25.00),
    "claude-opus-4-1": _p(15.00, 75.00),
    "claude-opus-4": _p(15.00, 75.00),
    "claude-sonnet-4": _p(3.00, 15.00),
    "claude-haiku-4-5": _p(1.00, 5.00),
    # Claude 3.x
    "claude-3-7-sonnet": _p(3.00, 15.00),
    "claude-3-5-sonnet": _p(3.00, 15.00),
    "claude-3-5-haiku": _p(0.80, 4.00),
    "claude-3-opus": _p(15.00, 75.00),
    "claude-3-sonnet": _p(3.00, 15.00),
    "claude-3-haiku": {"input": 0.25, "output": 1.25, "cache_write": 0.30, "cache_read": 0.03},
}

# Used when a model ID matches nothing above.
DEFAULT_MODEL = "claude-sonnet-4"


def prices_for(model: str) -> Prices:
    best = ""
    for prefix in PRICING:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return PRICING[best or DEFAULT_MODEL]


def cost_usd(model: str, usage: Mapping[str, int]) -> float:
    prices = prices_for(model)
    return (
        usage.get("input_tokens", 0) * prices["input"]
        + usage.get("output_tokens", 0) * prices["output"]
        + usage.get("cache_creation_input_tokens", 0) * prices["cache_write"]
        + usage.get("cache_read_input_tokens", 0) * prices["cache_read"]
    ) / 1_000_000
