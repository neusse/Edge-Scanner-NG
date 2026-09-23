#!/usr/bin/env python
"""Reference Edge alert consumer: reconnect, recover, deduplicate, log; never trade.

    python scripts/consume_alerts.py --url ws://localhost:7777/ws/alerts

The cursor file is local consumer state. A 410 response means the cursor is no
longer in the retained archive: stop and investigate rather than silently skip.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

log = logging.getLogger("edge-alert-consumer")


class RecoveryGap(RuntimeError):
    """The consumer cannot prove it saw every retained event."""


class AlertConsumer:
    def __init__(self, url: str, cursor_file: Path) -> None:
        self.url = url
        self.cursor_file = Path(cursor_file)
        self.cursor = self.cursor_file.read_text(encoding="utf-8").strip() if self.cursor_file.exists() else None
        self.seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self.last_session: str | None = None
        self.last_seq: int | None = None
        parsed = urlsplit(url)
        if parsed.scheme not in ("ws", "wss") or parsed.path != "/ws/alerts":
            raise ValueError("URL must point to ws(s)://host/ws/alerts")
        scheme = "https" if parsed.scheme == "wss" else "http"
        self.recovery_url = urlunsplit((scheme, parsed.netloc, "/api/alerts/recover", "", ""))
        self.filters = dict(parse_qsl(parsed.query))
        self.unfiltered = not any(k in self.filters for k in
                                  ("setups", "triggers", "symbols", "direction", "min_score", "custom")) \
            and self.filters.get("sources", "all").lower() in ("all", "*")

    def _remember(self, event_id: str) -> None:
        if event_id in self.seen:
            return
        self.seen.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > 10000:
            self.seen.discard(self._seen_order.popleft())

    def _save_cursor(self, event_id: str) -> None:
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        pending = self.cursor_file.with_suffix(self.cursor_file.suffix + ".tmp")
        pending.write_text(event_id + "\n", encoding="utf-8")
        os.replace(pending, self.cursor_file)
        self.cursor = event_id

    def accept(self, alert: dict) -> bool:
        """Return True only for a new live observation; no order action exists."""
        if alert.get("schema_version") != 1 or not alert.get("event_id"):
            raise RecoveryGap("missing v1 event identity")
        event_id = alert["event_id"]
        if event_id == self.cursor or event_id in self.seen:
            return False
        self._remember(event_id)
        self.last_session = alert.get("session_id")
        self.last_seq = alert.get("seq")
        if not alert.get("archive_write_ok", False):
            log.error("alert %s was not archived; a later disconnect may be unrecoverable", event_id)
        eligible = alert.get("mode") == "live"
        if not eligible:
            log.warning("NOT LIVE (%s): %s", alert.get("mode"), event_id)
        else:
            log.info("ALERT %s %s %s %s @ %s", event_id, alert.get("symbol"),
                     alert.get("setup"), alert.get("direction"), alert.get("price"))
        self._save_cursor(event_id)
        return eligible

    def recover(self) -> int:
        """Read retained alerts oldest first; fail closed if the cursor expired."""
        count = 0
        while True:
            params = {**self.filters, "limit": "500"}
            if self.cursor:
                params["after"] = self.cursor
            url = self.recovery_url + "?" + urlencode(params)
            try:
                with urlopen(url, timeout=10) as response:
                    page = json.load(response)
            except HTTPError as exc:
                if exc.code == 410:
                    raise RecoveryGap(f"cursor {self.cursor} expired or absent from retained archive") from exc
                raise
            before = self.cursor
            for alert in page.get("alerts", []):
                self.accept(alert)
                count += 1
            if not page.get("has_more"):
                return count
            if self.cursor == before:
                raise RecoveryGap("recovery page made no cursor progress")

    def process_frame(self, frame: dict) -> None:
        if frame.get("schema_version") != 1:
            raise RecoveryGap("unsupported feed schema version")
        kind = frame.get("type")
        if kind == "replay":
            if self.cursor or frame.get("truncated"):
                # REST is oldest-first and includes the race from the previous
                # recovery through this connection. Live frames follow replay.
                self.recover()
            else:
                for alert in reversed(frame.get("alerts", [])):  # newest first on the wire
                    self.accept(alert)
        elif kind == "alert":
            alert = frame["alert"]
            if (self.unfiltered and self.last_session == alert.get("session_id")
                    and self.last_seq is not None and alert.get("seq", 0) > self.last_seq + 1):
                self.recover()
            self.accept(alert)
        elif kind in ("heartbeat", "status"):
            status = frame.get("status") or {}
            if status.get("state") not in ("live",):
                log.warning("scanner state: %s", status.get("state"))
        else:
            raise RecoveryGap(f"unknown feed frame: {kind}")

    def run(self) -> None:
        from websockets.sync.client import connect
        from websockets.exceptions import WebSocketException

        backoff = 1.0
        while True:
            try:
                if self.cursor:
                    self.recover()
                with connect(self.url, open_timeout=10, close_timeout=2) as ws:
                    log.info("connected to %s", self.url)
                    backoff = 1.0
                    while True:
                        # Feed heartbeat is every 5 seconds. Three missed
                        # beats require a reconnect and archive recovery.
                        frame = json.loads(ws.recv(timeout=15))
                        self.process_frame(frame)
            except RecoveryGap:
                raise
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError, WebSocketException) as exc:
                log.warning("feed disconnected (%s); retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:7777/ws/alerts")
    parser.add_argument("--cursor-file", type=Path, default=Path("data/consumer-alert-cursor.txt"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        AlertConsumer(args.url, args.cursor_file).run()
    except RecoveryGap as exc:
        log.error("UNRECOVERABLE ALERT GAP: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
