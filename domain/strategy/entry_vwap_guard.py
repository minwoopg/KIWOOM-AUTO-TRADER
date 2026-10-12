"""Common entry-only VWAP consistency check; no exit policy changes."""
from __future__ import annotations

import math


def entry_vwap_block_reason(vwap, decision_price, live_price) -> str:
    """Both prices must exceed a valid VWAP. Missing live evidence fails closed."""
    values = (vwap, decision_price, live_price)
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v <= 0 for v in values):
        return "ENTRY_VWAP_UNAVAILABLE"
    if decision_price <= vwap or live_price <= vwap:
        return "ENTRY_BELOW_VWAP"
    return ""
