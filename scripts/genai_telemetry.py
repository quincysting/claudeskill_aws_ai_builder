"""Bedrock usage to OpenTelemetry GenAI token attributes, per-call cost, and telemetry writes that never
fail a request. Stdlib only. Ship this file next to the handler (for Lambda, at the zip root).

    from genai_telemetry import usage_attributes, cost_usd, best_effort
    attrs = usage_attributes(resp["usage"])          # resp from bedrock-runtime converse()
    span.set_attributes(attrs)
    cost = cost_usd(attrs, PRICES_PER_1K)              # your model's prices, from config
    best_effort("ledger", table.put_item, Item={...})  # a failed write is counted, not raised

Attribute names follow the OpenTelemetry GenAI registry
(opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/, checked 2026-11-08), which lists the cache
write count as gen_ai.usage.cache_creation.input_tokens. The registry marks the whole gen_ai.usage.* group
"moved to the OpenTelemetry GenAI repo", and a cache_write.input_tokens name seen there is not in the registry.
The conventions are still marked Development, so the names are pinned here and change in one place.
"""
from __future__ import annotations

import collections
import json
import logging
from typing import Any, Callable

INPUT = "gen_ai.usage.input_tokens"
OUTPUT = "gen_ai.usage.output_tokens"
CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
CACHE_WRITE = "gen_ai.usage.cache_creation.input_tokens"  # registry name; some drafts say cache_write
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")

DROPPED: collections.Counter[str] = collections.Counter()  # failed telemetry writes per sink, this process
_logger = logging.getLogger(__name__)


def usage_attributes(usage: dict[str, Any]) -> dict[str, int]:
    """Bedrock Converse ``usage`` -> OTel GenAI usage attributes.

    Bedrock's inputTokens EXCLUDES cached tokens (total input = inputTokens + cacheRead + cacheWrite).
    The convention's gen_ai.usage.input_tokens INCLUDES them and breaks reads and writes out separately, so
    uncached input (what the input price applies to) = INPUT - CACHE_READ - CACHE_WRITE.
    """
    for key in ("inputTokens", "outputTokens"):
        if usage.get(key) is None:
            raise ValueError(f"usage is missing {key}")
    read = int(usage.get("cacheReadInputTokens") or 0)
    write = int(usage.get("cacheWriteInputTokens") or 0)
    return {
        INPUT: int(usage["inputTokens"]) + read + write,
        OUTPUT: int(usage["outputTokens"]),
        CACHE_READ: read,
        CACHE_WRITE: write,
    }


def cost_usd(attrs: dict[str, int], price_per_1k: dict[str, float]) -> float:
    """Cost of one call from usage_attributes() output. price_per_1k needs input, output, cache_read and
    cache_write (USD per 1,000 tokens). Charging INPUT at the input price would bill cached tokens twice."""
    missing = [k for k in PRICE_KEYS if k not in price_per_1k]
    if missing:
        raise ValueError(f"price_per_1k is missing {missing}")
    read, write = attrs.get(CACHE_READ, 0), attrs.get(CACHE_WRITE, 0)
    uncached = attrs.get(INPUT, 0) - read - write
    if uncached < 0:
        raise ValueError("INPUT is smaller than the cache counts: pass usage_attributes() output, not raw usage")
    return (uncached * price_per_1k["input"] + attrs.get(OUTPUT, 0) * price_per_1k["output"]
            + read * price_per_1k["cache_read"] + write * price_per_1k["cache_write"]) / 1000.0


def best_effort(sink: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
    """Run one telemetry write (ledger row, metric, log shipment). Never raises into the request path:
    a failure is counted in DROPPED[sink] and logged as JSON, so a metric filter can alarm on it."""
    try:
        fn(*args, **kwargs)
        return True
    except Exception as exc:  # any telemetry failure is dropped and counted, never raised
        DROPPED[sink] += 1
        _logger.warning(json.dumps({"telemetry_dropped": sink, "error": type(exc).__name__}))
        return False
