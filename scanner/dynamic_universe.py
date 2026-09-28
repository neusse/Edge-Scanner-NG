"""Manual, fail-closed admission of Schwab discovery candidates.

Discovery, provider membership, history warmup, and setup eligibility are kept
as separate states.  A symbol joins only the setup plans whose required inputs
are certified for the current promotion epoch.
"""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import timedelta
from typing import Any

import pandas as pd


_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


class PromotionError(ValueError):
    pass


def _iso(epoch: float) -> str:
    return pd.Timestamp(epoch, unit="s", tz="UTC").isoformat()


def _bar_from_row(symbol: str, timestamp: Any, row: Any, source: str) -> dict:
    stamp = pd.Timestamp(timestamp)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return {
        "symbol": symbol, "timestamp": stamp, "source": source,
        "open": float(row["open"]), "high": float(row["high"]),
        "low": float(row["low"]), "close": float(row["close"]),
        "volume": float(row["volume"]),
    }


class ManualPromotionController:
    """Coordinates one-stream membership and one-shot cache-first warmup."""

    def __init__(self, scanner, feed, *, clock=time.time, cooldown_seconds: int = 60,
                 minimum_residence_seconds: int = 30 * 60) -> None:
        self.scanner, self.feed = scanner, feed
        self._clock = clock
        self._cooldown_seconds = max(1, int(cooldown_seconds))
        self._minimum_residence_seconds = max(0, int(minimum_residence_seconds))
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._buffers: dict[str, list[dict]] = {}
        self._workers: dict[str, int] = {}
        self._epoch = 0
        setter = getattr(feed, "set_dynamic_controller", None)
        if callable(setter):
            setter(self)
        readiness_setter = getattr(scanner, "set_dynamic_readiness_listener", None)
        if callable(readiness_setter):
            readiness_setter(self.on_readiness)

    def _record(self, symbol: str) -> dict:
        return self._records.setdefault(symbol, {
            "symbol": symbol, "state": "candidate", "reason": None,
            "requested_at": None, "acknowledged_at": None,
            "ready_at": None, "cooldown_until": None,
            "ready_epoch": None, "released_at": None,
            "chart": "not_requested", "level_one": "not_requested",
            "history": None, "merge": None, "readiness": None,
            "setup_evaluation": "disabled", "epoch": None,
            "manual_pin": False, "operator_hold": False,
            "connection_epoch": 0, "resume_state": None, "audit": [],
        })

    def _audit_locked(self, record: dict, action: str, **details) -> None:
        event = {"at": _iso(self._clock()), "action": action, **details}
        record.setdefault("audit", []).append(event)
        record["audit"] = record["audit"][-50:]

    def _protections_locked(self, symbol: str, record: dict) -> list[dict]:
        reasons: list[dict] = []
        support = symbol == "SPY" or symbol in set(getattr(self.scanner, "_sector_symbols", ()))
        if support:
            reasons.append({"code": "support", "label": "required reference symbol"})
        if record.get("manual_pin"):
            reasons.append({"code": "manual", "label": "manually pinned"})
        external = getattr(self.feed, "is_external_quote_watch", lambda _symbol: False)(symbol)
        if external:
            reasons.append({"code": "external_watch", "label": "external quote watch"})
        if record.get("state") in {"requested", "acknowledged", "warming"}:
            reasons.append({"code": "warming", "label": "admission is still warming"})
        ready_epoch = record.get("ready_epoch")
        if ready_epoch is not None:
            remaining = self._minimum_residence_seconds - (self._clock() - float(ready_epoch))
            if remaining > 0:
                reasons.append({"code": "minimum_residence", "label": "minimum residence",
                                "remaining_seconds": int(math.ceil(remaining))})
        if record.get("operator_hold"):
            reasons.append({"code": "operator_hold", "label": "operator hold"})
        return reasons

    def status(self, symbol: str) -> dict:
        symbol = str(symbol).upper().strip()
        with self._lock:
            record = dict(self._record(symbol))
            record["protections"] = self._protections_locked(symbol, record)
            record["protected"] = bool(record["protections"])
        capacity = getattr(self.feed, "dynamic_capacity", lambda: {})()
        record["capacity"] = capacity
        return record

    def statuses(self) -> dict[str, dict]:
        with self._lock:
            symbols = list(self._records)
        return {symbol: self.status(symbol) for symbol in symbols}

    def bar_epoch(self, symbol: str) -> int | None:
        """Cheap stream-path lookup that never creates a candidate record."""
        with self._lock:
            record = self._records.get(str(symbol).upper())
            return int(record["epoch"]) if record is not None and record.get("epoch") is not None else None

    def admit(self, symbol: str, candidate: dict | None = None) -> dict:
        symbol = str(symbol).upper().strip()
        if not _SYMBOL.fullmatch(symbol):
            raise PromotionError("unsupported equity symbol")
        if candidate is None:
            raise PromotionError("symbol is not in the current discovery session")
        price = candidate.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise PromotionError("candidate has no valid positive price") from None
        if not math.isfinite(price) or price <= 0:
            raise PromotionError("candidate has no valid positive price")
        with self._lock:
            record = self._record(symbol)
            cooldown = record.get("cooldown_until")
            if cooldown and self._clock() < float(cooldown):
                raise PromotionError("candidate is in retry cooldown")
            if record["state"] in {"requested", "acknowledged", "warming", "ready"}:
                return self.status(symbol)
            self._epoch += 1
            record.update({
                "state": "requested", "reason": None, "requested_at": _iso(self._clock()),
                "acknowledged_at": None, "ready_at": None, "cooldown_until": None,
                "ready_epoch": None, "released_at": None,
                "chart": "requested", "level_one": "requested",
                "history": None, "merge": None, "readiness": None,
                "setup_evaluation": "disabled", "epoch": self._epoch,
            })
            self._audit_locked(record, "admission_requested", epoch=self._epoch)
            self._buffers[symbol] = []
        try:
            self.feed.request_dynamic_membership(symbol)
        except Exception as exc:
            self._fail(symbol, str(exc))
            raise PromotionError(str(exc)) from exc
        return self.status(symbol)

    def protect(self, symbol: str, reason: str, enabled: bool) -> dict:
        """Set one operator-owned protection without disturbing other reasons."""
        symbol = str(symbol).upper().strip()
        field = {"manual": "manual_pin", "operator_hold": "operator_hold"}.get(reason)
        if field is None:
            raise PromotionError("protection reason must be manual or operator_hold")
        with self._lock:
            record = self._record(symbol)
            if record.get("state") in {"candidate", "released"}:
                raise PromotionError("symbol has no active dynamic lease")
            if record.get("state") == "releasing":
                raise PromotionError("symbol release is already in progress")
            record[field] = bool(enabled)
            self._audit_locked(record, "protection_changed", reason=reason, enabled=bool(enabled))
        return self.status(symbol)

    def release(self, symbol: str, *, automatic: bool = False) -> dict:
        """Begin an acknowledged, service-specific release of one dynamic lease."""
        symbol = str(symbol).upper().strip()
        with self._lock:
            record = self._record(symbol)
            if record.get("state") == "released":
                return self.status(symbol)
            if record.get("state") == "releasing":
                return self.status(symbol)
            if record.get("epoch") is None or record.get("state") in {"candidate", "failed"}:
                raise PromotionError("symbol has no releasable dynamic lease")
            protections = self._protections_locked(symbol, record)
            if protections:
                names = ", ".join(reason["code"] for reason in protections)
                raise PromotionError(f"symbol is protected: {names}")
            epoch = int(record["epoch"])
            self.scanner.begin_dynamic_release(symbol, epoch)
            record.update({"state": "releasing", "reason": None,
                           "setup_evaluation": "disabled",
                           "chart": "removal_requested", "level_one": "removal_requested",
                           "release_kind": "automatic" if automatic else "manual"})
            self._audit_locked(record, "release_requested", epoch=epoch,
                               kind="automatic" if automatic else "manual")
            try:
                self.feed.request_dynamic_release(symbol)
            except Exception as exc:
                record.update({"state": "release_failed", "reason": str(exc)})
                raise PromotionError(str(exc)) from exc
        return self.status(symbol)

    def protected_symbols(self) -> set[str]:
        with self._lock:
            return {
                symbol for symbol, record in self._records.items()
                if record.get("epoch") is not None and self._protections_locked(symbol, record)
            }

    def on_connection(self, active: bool, connection_epoch: int) -> None:
        """Invalidate active leases on disconnect; a reconnect must re-ack and rewarm."""
        if active:
            return
        with self._lock:
            for symbol, record in self._records.items():
                if record.get("epoch") is None or record.get("state") in {"candidate", "released", "failed"}:
                    continue
                old_epoch = int(record["epoch"])
                self._epoch += 1
                new_epoch = self._epoch
                releasing = record.get("state") in {"releasing", "release_failed", "release_reconnecting"}
                self.scanner.reconnect_dynamic_symbol(symbol, old_epoch, new_epoch)
                record.update({
                    "state": "release_reconnecting" if releasing else "reconnecting",
                    "resume_state": "release" if releasing else "admission",
                    "reason": "Schwab stream reconnect requires fresh acknowledgements",
                    "chart": "reconnecting", "level_one": "reconnecting",
                    "readiness": None, "setup_evaluation": "disabled",
                    "epoch": new_epoch, "connection_epoch": int(connection_epoch),
                })
                self._buffers[symbol] = []
                self._workers.pop(symbol, None)
                self._audit_locked(record, "connection_invalidated", old_epoch=old_epoch,
                                   epoch=new_epoch, connection_epoch=int(connection_epoch),
                                   resume=record["resume_state"])

    def on_membership(self, symbol: str, snapshot: dict) -> None:
        symbol = str(symbol).upper()
        start_epoch = None
        with self._lock:
            record = self._record(symbol)
            record["chart"] = snapshot.get("chart", record["chart"])
            record["level_one"] = snapshot.get("level_one", record["level_one"])
            if record["state"] in {"releasing", "release_failed", "release_reconnecting"}:
                error = snapshot.get("error")
                if error:
                    record.update({"state": "release_failed", "reason": str(error)})
                    self._audit_locked(record, "release_rejected", reason=str(error),
                                       chart=record["chart"], level_one=record["level_one"])
                    return
                if record["chart"] == "removed" and record["level_one"] == "removed":
                    epoch = int(record["epoch"])
                    self.scanner.finish_dynamic_release(symbol, epoch)
                    record.update({"state": "released", "released_at": _iso(self._clock()),
                                   "reason": None, "readiness": None,
                                   "setup_evaluation": "disabled"})
                    self._audit_locked(record, "release_acknowledged", epoch=epoch)
                    self._buffers.pop(symbol, None)
                return
            error = snapshot.get("error")
            if error:
                self._fail_locked(symbol, str(error))
                return
            if record["chart"] == "acknowledged" and record["level_one"] == "acknowledged":
                record["state"] = "acknowledged"
                record["acknowledged_at"] = _iso(self._clock())
                if self._workers.get(symbol) != record.get("epoch"):
                    start_epoch = int(record["epoch"])
                    self._workers[symbol] = start_epoch
                self._audit_locked(record, "membership_acknowledged",
                                   epoch=record.get("epoch"),
                                   connection_epoch=record.get("connection_epoch"))
        if start_epoch is not None:
            threading.Thread(target=self._warm, args=(symbol, start_epoch), daemon=True,
                             name=f"schwab-promote-{symbol}").start()

    def on_readiness(self, symbol: str, epoch: int | None, readiness: dict) -> None:
        """Receive later per-bar capability changes without accepting stale epochs."""
        symbol = str(symbol).upper()
        with self._lock:
            record = self._records.get(symbol)
            if record is None or record.get("epoch") != epoch or record["state"] != "ready":
                return
            record["readiness"] = readiness
            record["setup_evaluation"] = (
                "enabled" if readiness.get("ready_setup_ids") else "unavailable"
            )

    def on_bar(self, bar: dict) -> bool:
        """Buffer an admitted symbol until ready; return True when consumed."""
        symbol = str(bar.get("symbol") or "").upper()
        with self._lock:
            record = self._records.get(symbol)
            if record is not None and bar.get("_dynamic_epoch") not in (None, record.get("epoch")):
                self._audit_locked(record, "late_bar_rejected",
                                   bar_epoch=bar.get("_dynamic_epoch"), epoch=record.get("epoch"))
                return True
            if record is None or record["state"] in {"ready", "failed", "candidate"}:
                return False
            if record["state"] in {"releasing", "release_failed", "release_reconnecting", "released"}:
                return True
            self._buffers.setdefault(symbol, []).append(dict(bar))
            return True

    def _warm(self, symbol: str, epoch: int) -> None:
        try:
            with self._lock:
                record = self._record(symbol)
                if record.get("epoch") != epoch:
                    return
                record["state"] = "warming"
            today = pd.Timestamp.now(tz="America/New_York").date()
            before = getattr(self.feed, "history_stats", lambda: {})()
            daily = self.feed.get_historical_daily(symbol, today - timedelta(days=380), today)
            bars_5m = self.feed.get_historical_bars(symbol, "5Min", today - timedelta(days=20), today)
            session = self.feed.get_todays_bars(symbol, "1Min")
            after = getattr(self.feed, "history_stats", lambda: {})()
            if daily is None or daily.empty:
                raise PromotionError("daily history unavailable")
            session_bars = [
                _bar_from_row(symbol, stamp, row, "schwab_history_1m")
                for stamp, row in (session.iterrows() if session is not None else [])
            ]
            with self._lock:
                streamed = list(self._buffers.get(symbol, []))
                self._buffers[symbol] = []
            merged, evidence = self._merge(session_bars, streamed)
            if evidence["rth_gap_minutes"]:
                raise PromotionError(
                    f"session history has {evidence['rth_gap_minutes']} missing RTH minute(s)"
                )
            self.scanner.add_dynamic_symbol(
                symbol, daily, bars_5m, merged, evaluation_enabled=False,
                dynamic_epoch=epoch,
            )
            # Drain anything that arrived while state was being built. These
            # bars are still synchronization input and cannot emit setups.
            while True:
                with self._lock:
                    if self._record(symbol).get("epoch") != epoch:
                        raise PromotionError("stale promotion epoch")
                    pending = self._buffers.get(symbol, [])
                    self._buffers[symbol] = []
                    if not pending:
                        readiness = self.scanner.activate_dynamic_symbol(symbol, epoch)
                        record = self._record(symbol)
                        record.update({
                            "state": "ready", "ready_at": _iso(self._clock()), "reason": None,
                            "ready_epoch": self._clock(),
                            "history": {"before": before, "after": after,
                                        "daily_rows": len(daily), "profile_rows": len(bars_5m),
                                        "session_rows": len(session_bars)},
                            "merge": evidence,
                            "readiness": readiness,
                            "setup_evaluation": (
                                "enabled" if readiness.get("ready_setup_ids") else "unavailable"
                            ),
                        })
                        self._audit_locked(record, "readiness_confirmed", epoch=epoch,
                                           ready_setups=len(readiness.get("ready_setup_ids") or ()))
                        break
                for bar in sorted(pending, key=lambda item: pd.Timestamp(item["timestamp"])):
                    self.scanner.prime_stream_bar(bar)
        except Exception as exc:
            self._fail(symbol, str(exc))
        finally:
            with self._lock:
                if self._workers.get(symbol) == epoch:
                    self._workers.pop(symbol, None)

    @staticmethod
    def _merge(history: list[dict], streamed: list[dict]) -> tuple[list[dict], dict]:
        by_time: dict[pd.Timestamp, dict] = {}
        disagreements = 0
        duplicates = 0
        for bar in history:
            stamp = pd.Timestamp(bar["timestamp"]).tz_convert("UTC")
            if stamp in by_time:
                duplicates += 1
            by_time[stamp] = dict(bar)
        for bar in streamed:
            stamp = pd.Timestamp(bar["timestamp"])
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            previous = by_time.get(stamp)
            if previous is not None:
                duplicates += 1
                if any(abs(float(previous[key]) - float(bar[key])) > (1.0 if key == "volume" else 0.0001)
                       for key in ("open", "high", "low", "close", "volume")):
                    disagreements += 1
            replacement = dict(bar); replacement["timestamp"] = stamp
            by_time[stamp] = replacement  # current-epoch stream is authoritative
        ordered = [by_time[key] for key in sorted(by_time)]
        gaps = 0
        rth = []
        for bar in ordered:
            stamp = pd.Timestamp(bar["timestamp"]).tz_convert("America/New_York")
            minute = stamp.hour * 60 + stamp.minute
            if 570 <= minute < 960:
                rth.append(stamp.tz_convert("UTC"))
        for prior, current in zip(rth, rth[1:]):
            if current - prior > pd.Timedelta(minutes=1):
                gaps += int((current - prior) / pd.Timedelta(minutes=1)) - 1
        return ordered, {
            "history_rows": len(history), "stream_rows": len(streamed),
            "merged_rows": len(ordered), "deduplicated": duplicates,
            "material_disagreements": disagreements, "rth_gap_minutes": gaps,
            "stream_won_overlap": True,
        }

    def _fail(self, symbol: str, reason: str) -> None:
        with self._lock:
            self._fail_locked(symbol, reason)

    def _fail_locked(self, symbol: str, reason: str) -> None:
        record = self._record(symbol)
        record.update({
            "state": "failed", "reason": reason,
            "cooldown_until": self._clock() + self._cooldown_seconds,
        })
        self._audit_locked(record, "admission_failed", reason=reason,
                           chart=record.get("chart"), level_one=record.get("level_one"))
