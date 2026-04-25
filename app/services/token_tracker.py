"""
Token Cost Tracker — loguje zuzycie tokenow i szacunkowy koszt per request.
Gemini pricing (2026 est.):
  Flash input:  $0.075 / 1M tokens
  Flash output: $0.30  / 1M tokens
  Pro input:    $1.25  / 1M tokens
  Pro output:   $5.00  / 1M tokens
Google Search Grounding:
  $35 / 1000 grounded requests (po darmowym tier 1500/dzien)
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("token_tracker")

# Pricing per 1M tokens (USD)
PRICING = {
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-3.1-pro-preview": {"input": 1.25, "output": 5.00},
}
DEFAULT_PRICING = {"input": 0.075, "output": 0.30}  # Flash as default

# Koszt grounding Google Search ($ per pojedynczy request z tool)
GROUNDING_COST_USD = 0.035  # Po przekroczeniu 1500 free/dzien

# USD to PLN
USD_PLN = 4.05


@dataclass
class TokenUsage:
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_pln: float = 0.0
    operation: str = ""
    timestamp: str = ""


def log_token_usage(response, model: str, operation: str) -> TokenUsage:
    """Extract token counts from Gemini response and log cost estimate.
    Wykrywa tez grounding (Google Search) i dolicza $0.035 per request."""
    usage = TokenUsage(
        model=model,
        operation=operation,
        timestamp=datetime.utcnow().isoformat(),
    )

    try:
        # Gemini SDK returns usage_metadata on response
        meta = getattr(response, "usage_metadata", None)
        if meta:
            usage.input_tokens = getattr(meta, "prompt_token_count", 0) or 0
            usage.output_tokens = getattr(meta, "candidates_token_count", 0) or 0
        else:
            # Fallback: estimate from text length
            text = getattr(response, "text", "") or ""
            usage.output_tokens = len(text) // 4  # rough estimate

        pricing = PRICING.get(model, DEFAULT_PRICING)
        token_cost = (
            (usage.input_tokens / 1_000_000) * pricing["input"] +
            (usage.output_tokens / 1_000_000) * pricing["output"]
        )

        # Czy odpowiedz byla grounded (Google Search) — jesli tak, dolicz koszt
        grounding_cost = 0.0
        grounded = False
        try:
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                gmeta = getattr(candidates[0], "grounding_metadata", None)
                if gmeta:
                    chunks = getattr(gmeta, "grounding_chunks", None)
                    if chunks:
                        grounded = True
                        grounding_cost = GROUNDING_COST_USD
        except Exception:
            pass

        usage.cost_usd = token_cost + grounding_cost
        usage.cost_pln = usage.cost_usd * USD_PLN

        grounding_tag = " [🔍 GROUND]" if grounded else ""
        logger.info(
            f"[TOKENS]{grounding_tag} {operation} | {model} | "
            f"in={usage.input_tokens:,} out={usage.output_tokens:,} | "
            f"tokens=${token_cost:.4f}"
            f"{f' + grounding=${grounding_cost:.3f}' if grounded else ''}"
            f" | total=${usage.cost_usd:.4f} ({usage.cost_pln:.2f} PLN)"
        )
    except Exception as e:
        logger.warning(f"[TOKENS] Failed to track: {e}")

    return usage


# ── Accumulated session stats ──
_session_total_input = 0
_session_total_output = 0
_session_total_cost_usd = 0.0


def track_and_accumulate(response, model: str, operation: str) -> TokenUsage:
    """Track + add to session accumulator."""
    global _session_total_input, _session_total_output, _session_total_cost_usd
    usage = log_token_usage(response, model, operation)
    _session_total_input += usage.input_tokens
    _session_total_output += usage.output_tokens
    _session_total_cost_usd += usage.cost_usd
    return usage


def get_session_stats() -> dict:
    """Return accumulated stats for the current process."""
    return {
        "total_input_tokens": _session_total_input,
        "total_output_tokens": _session_total_output,
        "total_cost_usd": round(_session_total_cost_usd, 4),
        "total_cost_pln": round(_session_total_cost_usd * USD_PLN, 2),
    }
