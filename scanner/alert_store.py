"""Persist session alerts to daily JSONL files so they survive restarts."""
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

log = logging.getLogger(__name__)

_DEFAULT_DIR = Path("data/alerts")


class AlertStore:
    """Append-only store: one JSONL file per trading day under data/alerts/.

    On load, returns all alerts from the last `keep_days` calendar days,
    oldest first, so the in-memory buffer ends up in chronological order.
    On save, appends one JSON line per alert to today's file immediately.
    On cleanup (called at init), deletes files older than `keep_days`.
    """

    def __init__(self, store_dir: Path = _DEFAULT_DIR, keep_days: int = 5) -> None:
        self._dir      = Path(store_dir)
        self._keep_days = keep_days
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cleanup()

    # ── public API ────────────────────────────────────────────────────────────

    def save(self, alert: dict) -> bool:
        """Append a single alert to today's file; report whether it is durable."""
        path = self._dir / f"{date.today().isoformat()}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(alert) + "\n")
            return True
        except OSError as exc:
            log.warning("AlertStore: failed to write alert: %s", exc)
            return False

    def load_recent(self) -> list[dict]:
        """Return all alerts from the last keep_days days, oldest first."""
        cutoff = date.today() - timedelta(days=self._keep_days - 1)
        alerts: list[dict] = []
        for path in sorted(self._dir.glob("*.jsonl")):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_date < cutoff:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    for line_number, line in enumerate(f, 1):
                        line = line.strip()
                        if line:
                            alert = json.loads(line)
                            # Legacy records predate the v1 envelope. Their
                            # identity is stable for this archive location,
                            # but their live/replay provenance is unknowable.
                            if "event_id" not in alert:
                                alert["event_id"] = "legacy:" + uuid5(
                                    NAMESPACE_URL, f"{path.name}:{line_number}").hex
                                alert["mode"] = "unknown"
                                alert["schema_version"] = 1
                                alert["session_id"] = "legacy"
                                alert.setdefault("seq", line_number)
                                alert.setdefault("source", "unknown")
                                alert["market_timestamp"] = alert.get("timestamp")
                                alert["emitted_at"] = None
                                alert["archive_write_ok"] = True
                            alerts.append(alert)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("AlertStore: failed to read %s: %s", path.name, exc)
        log.info("AlertStore: loaded %d alerts from last %d days", len(alerts), self._keep_days)
        return alerts

    # ── private ───────────────────────────────────────────────────────────────

    def _cleanup(self) -> None:
        cutoff = date.today() - timedelta(days=self._keep_days - 1)
        for path in self._dir.glob("*.jsonl"):
            try:
                if date.fromisoformat(path.stem) < cutoff:
                    path.unlink()
                    log.debug("AlertStore: removed old file %s", path.name)
            except (ValueError, OSError):
                pass
