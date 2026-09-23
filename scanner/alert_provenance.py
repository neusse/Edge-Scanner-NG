"""Stable, additive detector and input-bar identities for alert consumers."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def custom_revision(setup: dict) -> str:
    """Identity of the exact saved setup document used by the evaluator."""
    return "custom-sha256:" + _digest(setup)


def effective_revision(base_revision: str, profile: Any = None,
                       parameters: Any = None, config_hash: str | None = None,
                       settings_values: dict | None = None) -> str:
    """Hash the setup plus every mutable gate snapshot used at evaluation."""
    return "detector-sha256:" + _digest({
        "base": base_revision,
        "profile": {"id": profile.id, "conditions": [*profile.static, *profile.dynamic]}
                   if profile else None,
        "parameters": parameters or [],
        "settings": config_hash,
        "settings_values": settings_values or {},
    })


@lru_cache(maxsize=16)
def _implementation_digest(module: str) -> str | None:
    if not module:
        return None
    try:
        import importlib.util
        spec = importlib.util.find_spec(module)
        if spec and spec.origin and Path(spec.origin).is_file():
            return hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()
    except (ImportError, OSError, ValueError):
        pass
    return None


def system_revision(code: str, config_hash: str | None, evaluator: Any = None) -> str:
    """System setup code, live settings and installed evaluator implementation.

    An unavailable implementation is explicit, rather than guessed from the
    current catalog. The catalog can supply the same evaluator when running.
    """
    module = getattr(type(evaluator), "__module__", "") if evaluator is not None else ""
    implementation = _implementation_digest(module)
    return "system-sha256:" + _digest({"setup": code, "settings": config_hash,
                                        "module": module, "implementation": implementation})


def source_bar(bar: dict) -> dict:
    """Identity of the evaluated 1-minute input, not an execution quote.

    `complete` is unknown for an unlabelled input. Stream adapters label their
    own bars; historical/test callers are not silently promoted to complete.
    """
    raw_source = bar.get("source")
    source = ({"schwab_chart_equity": "schwab_chart_equity",
               "alpaca_minute_bar": "alpaca_minute_bar",
               "quotes": "schwab_quote_derived"}.get(raw_source) or "unknown")
    ts = pd.Timestamp(bar["timestamp"])
    market_time = ts.tz_convert("UTC").isoformat() if ts.tzinfo is not None else None
    symbol = str(bar.get("symbol") or "").upper()
    return {"id": f"{source}:{symbol}:1m:{market_time}" if market_time and symbol else None,
            "source": source, "symbol": symbol or None, "timeframe": "1m",
            "market_timestamp": market_time,
            "complete": True if source != "unknown" else None}
