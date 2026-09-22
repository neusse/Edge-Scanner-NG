"""Alert sink — collects, deduplicates, and ranks scanner alerts.

Alerts are deduplicated per (symbol, direction, trigger) key within a
configurable cooldown window so the same setup can't spam the sink on
consecutive bars.  The collection is sorted by score on demand.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

_DEFAULT_COOLDOWN_MINUTES = 5
_DEFAULT_MAX_SIZE = 5000


class AlertSink:
    """Thread-safe-enough for single-threaded live loop; no locking needed."""

    def __init__(
        self,
        cooldown_minutes: int = _DEFAULT_COOLDOWN_MINUTES,
        max_size: int = _DEFAULT_MAX_SIZE,
    ) -> None:
        self._cooldown = pd.Timedelta(minutes=cooldown_minutes)
        self._max_size = max_size
        self._alerts: list[dict] = []
        self._last_ts: dict[tuple, pd.Timestamp] = {}  # key -> last accepted timestamp

    # ── Public API ────────────────────────────────────────────────────────────

    def push(self, alert: dict) -> bool:
        """Accept an alert, applying cooldown deduplication.

        Args:
            alert: alert dict produced by the evaluator (must have
                   symbol, direction, trigger, timestamp, score keys)

        Returns:
            True  — alert accepted and stored
            False — rejected (same setup within cooldown window)
        """
        key = (alert["symbol"], alert["direction"], alert["trigger"])
        now = pd.Timestamp(alert["timestamp"])

        last = self._last_ts.get(key)
        elapsed = now - last if last is not None else None
        if elapsed is not None and pd.Timedelta(0) <= elapsed < self._cooldown:
            log.debug("Sink: cooldown active for %s — skipping", key)
            return False

        self._last_ts[key] = now
        self._alerts.append(alert)
        log.debug("Sink: accepted alert %s score=%d", key, alert.get("score", 0))

        if len(self._alerts) > self._max_size:
            self._trim()

        return True

    def restore_cooldowns(self, alerts: list[dict]) -> None:
        """Restore accepted-alert timestamps without replaying visible alerts.

        The unified archive contains only alerts that their producer sink
        accepted.  Rebuilding this small index at startup preserves cooldown
        behavior across a process restart while leaving the sink's live alert
        collection empty.
        """
        for alert in alerts:
            try:
                key = (alert["symbol"], alert["direction"], alert["trigger"])
                timestamp = pd.Timestamp(alert["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            previous = self._last_ts.get(key)
            if previous is None or timestamp > previous:
                self._last_ts[key] = timestamp

    def top(self, n: int = 10) -> list[dict]:
        """Return the top-n alerts sorted by score descending."""
        return sorted(self._alerts, key=lambda a: a["score"], reverse=True)[:n]

    def since(self, timestamp: pd.Timestamp) -> list[dict]:
        """Return all alerts with timestamp >= given value, score-ranked."""
        recent = [a for a in self._alerts if pd.Timestamp(a["timestamp"]) >= timestamp]
        return sorted(recent, key=lambda a: a["score"], reverse=True)

    def all(self) -> list[dict]:
        """Return a copy of all stored alerts (unsorted)."""
        return list(self._alerts)

    def clear(self) -> None:
        """Remove all alerts and reset cooldown state (call at session end)."""
        self._alerts.clear()
        self._last_ts.clear()

    def __len__(self) -> int:
        return len(self._alerts)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _trim(self) -> None:
        """Keep only the highest-scoring alerts when over capacity."""
        self._alerts.sort(key=lambda a: a["score"], reverse=True)
        self._alerts = self._alerts[: self._max_size]
