"""Schwab SCREENER_EQUITY keys and observation-only candidate state."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from scanner.discovery_sessions import DiscoverySessionStore


SCREENER_MARKETS = {"EQUITY_ALL": "All equities", "NYSE": "NYSE", "NASDAQ": "Nasdaq",
                    "OTCBB": "OTC Bulletin Board", "INDEX_AL": "All indexes",
                    "$COMPX": "Nasdaq Composite", "$DJI": "Dow Jones", "$SPX.X": "S&P 500"}
SCREENER_MEASURES = {"PERCENT_CHANGE_UP": "Percent change up", "PERCENT_CHANGE_DOWN": "Percent change down",
                     "VOLUME": "Volume", "TRADES": "Trades", "AVERAGE_PERCENT_VOLUME": "Average percent volume"}
SCREENER_PERIODS = (0, 1, 5, 10, 30, 60)


def build_screener_key(market: str, measure: str, period: int | str) -> str:
    market, measure = str(market).strip().upper(), str(measure).strip().upper()
    try:
        period = int(period)
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported Schwab screener period") from exc
    if market not in SCREENER_MARKETS:
        raise ValueError(f"unsupported Schwab screener market: {market}")
    if measure not in SCREENER_MEASURES:
        raise ValueError(f"unsupported Schwab screener measure: {measure}")
    if period not in SCREENER_PERIODS:
        raise ValueError(f"unsupported Schwab screener period: {period}")
    return f"{market}_{measure}_{period}"


def validate_screener_key(key: str) -> str:
    candidate = str(key).strip().upper()
    valid = {build_screener_key(m, s, p) for m in SCREENER_MARKETS for s in SCREENER_MEASURES for p in SCREENER_PERIODS}
    if candidate not in valid:
        raise ValueError(f"unsupported Schwab screener key: {candidate}")
    return candidate


DEFAULT_SCREENER_KEYS = (
    "EQUITY_ALL_PERCENT_CHANGE_UP_1", "EQUITY_ALL_PERCENT_CHANGE_UP_5",
    "EQUITY_ALL_PERCENT_CHANGE_UP_10", "EQUITY_ALL_PERCENT_CHANGE_UP_60",
    "EQUITY_ALL_VOLUME_5", "EQUITY_ALL_TRADES_5", "EQUITY_ALL_AVERAGE_PERCENT_VOLUME_5",
)
DEFAULT_SCREENER_KEY = DEFAULT_SCREENER_KEYS[1]


def screener_catalog() -> dict[str, Any]:
    return {"markets": [{"value": v, "label": l} for v, l in SCREENER_MARKETS.items()],
            "measures": [{"value": v, "label": l} for v, l in SCREENER_MEASURES.items()],
            "periods": [{"value": p, "label": "All day" if p == 0 else f"{p} min"} for p in SCREENER_PERIODS],
            "defaults": list(DEFAULT_SCREENER_KEYS)}


def _number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _iso_ms(value: Any) -> str | None:
    number = _number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(float(number) / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _empty_list() -> dict[str, Any]:
    return {"rows": [], "error": None, "provider_epoch": None, "provider_timestamp": None,
            "receipt_epoch": None, "receipt_timestamp": None}


class SchwabScreenerBook:
    """Latest list snapshots plus a merged, session-scoped candidate registry."""

    def __init__(self, clock=time.time,
                 session_store: DiscoverySessionStore | None = None) -> None:
        self._clock, self._lock = clock, threading.RLock()
        self._session_store = session_store
        self._requested: list[str] = []
        self._lists: dict[str, dict[str, Any]] = {}
        self._candidates: dict[str, dict[str, Any]] = {}
        self._membership_decisions: dict[str, list[dict]] = {}
        self._requests: dict[str, list[str]] = {}
        self._connected = False
        self._last_activity: float | None = None
        self._error: str | None = None
        self._session_date: str | None = None
        self._session_status = "open"
        self._session_opened_at: str | None = None
        self._session_updated_at: str | None = None
        self._session_closed_at: str | None = None
        self._final_reason: str | None = None
        if self._session_store is not None:
            self._restore_active(self._session_store.load_active())

    def _restore_active(self, session: dict | None) -> None:
        if not session:
            return
        self._session_date = session["session_date"]
        self._session_status = "open"
        self._session_opened_at = session.get("opened_at")
        self._session_updated_at = session.get("updated_at")
        self._session_closed_at = None
        for row in session.get("candidates") or []:
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            contributions = row.get("contributing_lists") or []
            candidate = {key: row.get(key) for key in (
                "symbol", "description", "provider_timestamp", "price", "net_change",
                "percent_change", "volume", "total_volume", "trades", "market_share",
                "first_seen", "last_seen", "recurrence")}
            candidate["lists"] = {
                item["list_key"]: dict(item) for item in contributions
                if isinstance(item, dict) and item.get("list_key")
            }
            self._candidates[str(row["symbol"]).upper()] = candidate
            self._membership_decisions[str(row["symbol"]).upper()] = [
                dict(item) for item in (row.get("membership_decisions") or [])
                if isinstance(item, dict)
            ]

    @staticmethod
    def _date_at(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(
            ZoneInfo("America/New_York")).date().isoformat()

    @staticmethod
    def _iso_at(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    def _ensure_session(self, received: float) -> bool:
        session_date = self._date_at(received)
        if self._session_date == session_date:
            return self._session_status == "open"
        if self._session_date is not None and self._candidates:
            self.finalize("session_rollover", closed_at=self._iso_at(received))
        self._candidates = {}
        self._membership_decisions = {}
        for state in self._lists.values():
            state.update(_empty_list())
        self._session_date = session_date
        self._session_status = "open"
        self._session_opened_at = self._iso_at(received)
        self._session_updated_at = self._session_opened_at
        self._session_closed_at = None
        self._final_reason = None
        return True

    def _session_document(self, status: str | None = None,
                          final_reason: str | None = None,
                          closed_at: str | None = None) -> dict:
        candidates = []
        for candidate in self._candidates.values():
            row = self._candidate_row(candidate)
            row.update({
                "source": "schwab_screener_equity",
                "eligibility": {"status": "not_evaluated", "reasons": []},
                "membership_decisions": [dict(item) for item in
                                         self._membership_decisions.get(row["symbol"], [])],
            })
            candidates.append(row)
        candidates.sort(key=lambda row: (row["best_rank"] or 10_000, row["symbol"]))
        return {
            "schema_version": 1, "source": "schwab_screener_equity",
            "session_date": self._session_date,
            "status": status or self._session_status,
            "opened_at": self._session_opened_at,
            "updated_at": self._session_updated_at,
            "closed_at": closed_at if closed_at is not None else self._session_closed_at,
            "final_reason": final_reason,
            "requested_keys": list(self._requested),
            "candidates": candidates,
        }

    def _persist_active(self) -> None:
        if self._session_store is not None and self._session_date is not None:
            self._session_store.save_active(self._session_document("open"))

    def finalize(self, reason: str = "market_close", closed_at: str | None = None) -> dict | None:
        with self._lock:
            if self._session_date is None:
                return None
            if self._session_status == "final":
                return self._session_document("final", self._final_reason, closed_at)
            self._session_status = "final"
            self._final_reason = str(reason)
            self._session_closed_at = closed_at or self._iso_at(self._clock())
            record = self._session_document("final", self._final_reason, self._session_closed_at)
            if self._session_store is not None:
                self._session_store.finalize(record)
            return record

    def sessions(self) -> list[dict]:
        """Finalized sessions, newest first, for after-hours review."""
        return self._session_store.list_sessions() if self._session_store is not None else []

    def session(self, session_date: str) -> dict | None:
        """One current or finalized normalized discovery session."""
        with self._lock:
            if self._session_date == str(session_date):
                return self._session_document(
                    self._session_status, self._final_reason, self._session_closed_at,
                )
        return self._session_store.get(str(session_date)) if self._session_store is not None else None

    def record_membership_decision(
        self, session_date: str, symbols: list[str], decision: dict,
    ) -> dict | None:
        """Record an explicit save decision without changing scanner membership."""
        normalized = {str(symbol).upper() for symbol in symbols}
        with self._lock:
            if self._session_date == str(session_date):
                for symbol in normalized:
                    if symbol in self._candidates:
                        self._membership_decisions.setdefault(symbol, []).append(dict(decision))
                document = self._session_document(
                    self._session_status, self._final_reason, self._session_closed_at,
                )
                if self._session_store is not None:
                    if self._session_status == "final":
                        self._session_store.finalize(document)
                    else:
                        self._session_store.save_active(document)
                return document
        if self._session_store is not None:
            return self._session_store.record_membership_decision(
                str(session_date), sorted(normalized), decision,
            )
        return None

    def requested(self, keys: Iterable[str]) -> None:
        normalized = [validate_screener_key(key) for key in keys]
        with self._lock:
            self._requested = list(dict.fromkeys(normalized))
            for key in self._requested:
                self._lists.setdefault(key, _empty_list())

    def connection(self, active: bool) -> None:
        with self._lock:
            self._connected = bool(active)

    def expect(self, request_id: Any, keys: Iterable[str]) -> None:
        """Associate a provider response id with exactly the lists it controls."""
        if request_id is None:
            return
        with self._lock:
            self._requests[str(request_id)] = [validate_screener_key(key) for key in keys]

    def stream_activity(self) -> None:
        with self._lock:
            self._last_activity = self._clock()

    def ingest(self, raw: Any) -> None:
        """Accept one schwabdev callback payload. Malformed data is isolated."""
        self.stream_activity()
        try:
            message = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(message, dict):
                return
            self._ingest_responses(message.get("response"))
            for block in message.get("data") or []:
                if isinstance(block, dict) and block.get("service") == "SCREENER_EQUITY":
                    for content in block.get("content") or []:
                        self._ingest_content(content)
        except Exception:
            return

    def _ingest_responses(self, responses: Any) -> None:
        for response in responses or []:
            if not isinstance(response, dict) or response.get("service") != "SCREENER_EQUITY":
                continue
            content = response.get("content") or {}
            code = _number(content.get("code"))
            key = content.get("key") or response.get("key")
            error = None if code == 0 else str(content.get("msg") or f"Schwab response {code}")
            with self._lock:
                request_keys = self._requests.pop(str(response.get("requestid")), [])
                targets = [str(key).strip().upper()] if key else request_keys
                if targets:
                    for normalized in targets:
                        state = self._lists.setdefault(normalized, _empty_list())
                        state["error"] = error
                else:
                    self._error = error

    def _ingest_content(self, content: Any) -> None:
        if not isinstance(content, dict):
            return
        try:
            key = validate_screener_key(str(content.get("key") or ""))
        except ValueError:
            return
        items = content.get("4")
        if not isinstance(items, list):
            return
        provider_epoch = _number(content.get("1"))
        received = self._clock()
        received_iso = datetime.fromtimestamp(received, tz=timezone.utc).isoformat()
        with self._lock:
            if not self._ensure_session(received):
                return
            previous = self._lists.get(key)
            previous_epoch = previous and previous.get("provider_epoch")
            if provider_epoch is not None and previous_epoch is not None and provider_epoch <= previous_epoch:
                return
            for candidate in self._candidates.values():
                contribution = candidate["lists"].get(key)
                if contribution is not None:
                    contribution["current_rank"] = None
            rows: list[dict[str, Any]] = []
            for rank, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                row = {"list_key": key, "rank": rank, "symbol": symbol, "description": item.get("description"),
                       "provider_timestamp": _iso_ms(provider_epoch), "price": _number(item.get("lastPrice")),
                       "net_change": _number(item.get("netChange")), "percent_change": _number(item.get("netPercentChange")),
                       "volume": _number(item.get("volume")), "total_volume": _number(item.get("totalVolume")),
                       "trades": _number(item.get("trades")), "market_share": _number(item.get("marketShare"))}
                rows.append(row)
                candidate = self._candidates.setdefault(symbol, {"symbol": symbol, "first_seen": received_iso,
                    "last_seen": received_iso, "recurrence": 0, "lists": {}})
                candidate.update({name: row[name] for name in ("description", "provider_timestamp", "price", "net_change",
                    "percent_change", "volume", "total_volume", "trades", "market_share")})
                candidate["last_seen"], candidate["recurrence"] = received_iso, candidate["recurrence"] + 1
                contribution = candidate["lists"].get(key)
                candidate["lists"][key] = {"list_key": key, "current_rank": rank,
                    "best_rank": rank if contribution is None else min(rank, contribution["best_rank"]),
                    "first_seen": received_iso if contribution is None else contribution["first_seen"],
                    "last_seen": received_iso, "recurrence": 1 if contribution is None else contribution["recurrence"] + 1}
            self._lists[key] = {"provider_epoch": provider_epoch, "provider_timestamp": _iso_ms(provider_epoch),
                "receipt_epoch": received, "receipt_timestamp": received_iso, "error": None, "rows": rows}
            self._error = None
            self._session_updated_at = received_iso
            self._persist_active()

    def _list_health(self, key: str, now: float) -> dict[str, Any]:
        state = self._lists.get(key) or {}
        receipt, error = state.get("receipt_epoch"), state.get("error")
        status = "error" if error else ("live" if self._connected else "disconnected") if receipt is not None else ("waiting" if self._connected else "disconnected")
        return {"list_key": key, "status": status, "error": error,
                "provider_timestamp": state.get("provider_timestamp"), "receipt_timestamp": state.get("receipt_timestamp"),
                "receipt_age_ms": None if receipt is None else max(0, int((now - receipt) * 1000)),
                "row_count": len(state.get("rows") or [])}

    @staticmethod
    def _candidate_row(candidate: dict[str, Any]) -> dict[str, Any]:
        contributions = sorted(candidate["lists"].values(), key=lambda item: item["list_key"])
        current = [item["current_rank"] for item in contributions if item["current_rank"] is not None]
        best = [item["best_rank"] for item in contributions]
        fields = ("symbol", "description", "provider_timestamp", "price", "net_change", "percent_change",
                  "volume", "total_volume", "trades", "market_share", "first_seen", "last_seen", "recurrence")
        return {**{name: candidate.get(name) for name in fields}, "list_key": "combined",
                "rank": min(current) if current else None, "current_rank": min(current) if current else None,
                "best_rank": min(best) if best else None, "contributing_lists": contributions}

    def snapshot(self, key: str | None = None) -> dict[str, Any]:
        with self._lock:
            chosen = key or "combined"
            if chosen != "combined":
                try:
                    chosen = validate_screener_key(chosen)
                except ValueError:
                    chosen = "combined"
            now = self._clock()
            lists = [self._list_health(k, now) for k in self._requested]
            if chosen == "combined":
                rows = [self._candidate_row(candidate) for candidate in self._candidates.values()]
                rows.sort(key=lambda row: (row["current_rank"] is None, row["current_rank"] or 10_000,
                                           row["best_rank"] or 10_000, row["symbol"]))
            else:
                rows = []
                for raw in (self._lists.get(chosen) or {}).get("rows") or []:
                    candidate = self._candidates.get(raw["symbol"])
                    rows.append({**(self._candidate_row(candidate) if candidate else {}), **raw})
            selected = None if chosen == "combined" else self._list_health(chosen, now)
            live_lists, error_lists = [x for x in lists if x["status"] == "live"], [x for x in lists if x["status"] == "error"]
            status = "error" if self._error else selected["status"] if selected else "live" if live_lists else "error" if error_lists and len(error_lists) == len(lists) else "waiting" if self._connected else "disconnected"
            receipt_ages = [x["receipt_age_ms"] for x in lists if x["receipt_age_ms"] is not None]
            return {"mode": "observe", "source": "schwab_screener_equity", "status": status,
                    "connected": self._connected, "requested_keys": list(self._requested), "list_key": chosen,
                    "provider_timestamp": selected and selected["provider_timestamp"],
                    "receipt_timestamp": selected and selected["receipt_timestamp"],
                    "receipt_age_ms": selected["receipt_age_ms"] if selected else (min(receipt_ages) if receipt_ages else None),
                    "stream_activity_age_ms": None if self._last_activity is None else max(0, int((now - self._last_activity) * 1000)),
                    "error": self._error, "lists": lists, "rows": [dict(row) for row in rows],
                    "session": {"session_date": self._session_date,
                                "status": self._session_status,
                                "opened_at": self._session_opened_at,
                                "updated_at": self._session_updated_at,
                                "closed_at": self._session_closed_at,
                                "final_reason": self._final_reason}}
