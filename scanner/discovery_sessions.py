"""Bounded persistence for normalized Schwab discovery sessions."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from scanner.json_store import AtomicJsonStore

log = logging.getLogger(__name__)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, separators=(",", ":"), default=str))


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class DiscoverySessionStore:
    """Final JSONL sessions plus one atomically updated active snapshot.

    A torn final append can only affect the last line and is ignored on read;
    the next successful finalize rewrites the bounded valid set atomically.
    """

    def __init__(self, path: Path = Path("data/schwab_discovery/sessions.jsonl"),
                 retention_sessions: int = 20) -> None:
        self.path = Path(path)
        self.active = AtomicJsonStore(
            self.path.with_name(f"{self.path.stem}.active.json")
        )
        self.retention_sessions = max(1, min(100, int(retention_sessions)))
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("DiscoverySessionStore: read failed: %s", exc)
            return []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                if index != len(lines) - 1:
                    log.warning("DiscoverySessionStore: skipping corrupt line %d", index + 1)
                continue
            if (isinstance(row, dict) and row.get("status") == "final"
                    and isinstance(row.get("session_date"), str)
                    and isinstance(row.get("candidates"), list)):
                rows.append(row)
        by_date = {row["session_date"]: row for row in rows}
        return [by_date[key] for key in sorted(by_date)]

    def list_sessions(self) -> list[dict]:
        with self._lock:
            return [{
                "session_date": row["session_date"], "status": "final",
                "opened_at": row.get("opened_at"), "closed_at": row.get("closed_at"),
                "final_reason": row.get("final_reason"),
                "candidate_count": len(row.get("candidates") or []),
            } for row in reversed(self._load())]

    def get(self, session_date: str) -> dict | None:
        with self._lock:
            row = next((item for item in self._load()
                        if item["session_date"] == str(session_date)), None)
            return _copy(row) if row is not None else None

    def load_active(self) -> dict | None:
        row = self.active.read()
        if (not isinstance(row, dict) or row.get("status") != "open"
                or not isinstance(row.get("session_date"), str)
                or not isinstance(row.get("candidates"), list)):
            return None
        return _copy(row)

    def save_active(self, session: dict) -> None:
        if session.get("status") != "open":
            raise ValueError("active discovery session must be open")
        self.active.write(_copy(session))

    def finalize(self, session: dict) -> dict:
        record = _copy(session)
        record["status"] = "final"
        with self._lock:
            rows = [row for row in self._load()
                    if row["session_date"] != record.get("session_date")]
            rows.append(record)
            rows.sort(key=lambda row: row["session_date"])
            rows = rows[-self.retention_sessions:]
            _write_jsonl_atomic(self.path, rows)
            # A final record is authoritative even if deleting the active
            # convenience snapshot is interrupted.
            try:
                self.active.path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("DiscoverySessionStore: active cleanup failed: %s", exc)
        return record

    def record_membership_decision(
        self, session_date: str, symbols: list[str], decision: dict,
    ) -> dict | None:
        """Attach an explicit watchlist/membership decision to retained candidates."""
        with self._lock:
            record = self.get(session_date)
            if record is None:
                return None
            wanted = {str(symbol).upper() for symbol in symbols}
            for candidate in record.get("candidates") or []:
                if str(candidate.get("symbol") or "").upper() in wanted:
                    candidate.setdefault("membership_decisions", []).append(_copy(decision))
            return self.finalize(record)
