"""Adapter registry, keyed on an endpoint's ``protocol`` column.

``protocol`` is the *only* dispatch key. ``kind`` describes network topology
and is re-derived on every PATCH, and ``server_type`` is a cosmetic badge that
no backend code reads — neither can carry protocol semantics.
"""

from app.services.adapters.base import (
    HOP_BY_HOP,
    RetryableUpstreamError,
    StaleModelOverride,
    UpstreamAdapter,
    UpstreamSaturated,
    classify_httpx_error,
)
from app.services.adapters.anthropic import AnthropicAdapter
from app.services.adapters.gemini import GeminiAdapter
from app.services.adapters.opencode import OpenCodeAdapter
from app.services.adapters.passthrough import PassthroughAdapter

PASSTHROUGH = PassthroughAdapter()
OPENCODE = OpenCodeAdapter()
ANTHROPIC = AnthropicAdapter()
GEMINI = GeminiAdapter()

_BY_PROTOCOL: dict[str, UpstreamAdapter] = {
    "openai": PASSTHROUGH,
    "opencode": OPENCODE,
    "anthropic": ANTHROPIC,
    "gemini": GEMINI,
}


def get_adapter(endpoint: dict) -> UpstreamAdapter:
    """Adapter for an endpoint row. Unknown/missing protocol falls back to
    passthrough so a row written by an older build keeps working."""
    return _BY_PROTOCOL.get(endpoint.get("protocol") or "openai", PASSTHROUGH)


__all__ = [
    "HOP_BY_HOP",
    "OPENCODE",
    "PASSTHROUGH",
    "OpenCodeAdapter",
    "PassthroughAdapter",
    "RetryableUpstreamError",
    "StaleModelOverride",
    "UpstreamAdapter",
    "UpstreamSaturated",
    "classify_httpx_error",
    "get_adapter",
]
