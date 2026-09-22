"""Unified alert feed: every producer (the custom setups, and system setups
when an engine plugin provides them) publishes into ONE hub, and every
consumer subscribes to ONE WebSocket with the filter it wants.

    ws://host:7777/ws/alerts                             everything
    ws://host:7777/ws/alerts?sources=custom              one producer
    ws://host:7777/ws/alerts?setups=CODE1,CODE2          two setups
    ws://host:7777/ws/alerts?symbols=AAPL,NVDA           per-symbol
    GET  /api/alerts?...same filters...&limit=200        recent, newest first

Every alert carries `source` ("system" | "custom"). Wire format:
{"type": "replay", "alerts": [...]} on connect (newest first, already
filtered), then {"type": "alert", "alert": {...}} per alert.

Archive: data/alerts/all/YYYY-MM-DD.jsonl, one line per alert with `source`,
kept `keep_days` days. A producer's own sink may keep a separate archive.

The hub is fed through `tap()`: it wraps an existing AlertSink so whatever
else that sink feeds keeps working unchanged and the hub sees every accepted
alert.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from scanner.alert_sink import AlertSink
from scanner.alert_store import AlertStore

log = logging.getLogger(__name__)

SOURCES = ("system", "custom")
_POLL = 0.05
# Slow-subscriber isolation. Every client gets its own bounded outbox and sender
# task, so one stalled socket cannot delay anyone else. A client that falls
# _CLIENT_QUEUE messages behind, or whose socket does not take a message within
# _SEND_TIMEOUT seconds, is disconnected rather than silently skipped: it
# reconnects, gets the replay, and dedupes on `seq`, so nothing is lost quietly.
_CLIENT_QUEUE = 1000
_SEND_TIMEOUT = 5.0
# Fan-out is chunked so a burst bigger than an outbox cannot make a HEALTHY client
# look slow: after each chunk the loop yields so senders drain, and waits briefly
# (bounded) while any outbox is over half full. A stalled client still fills up
# and is dropped; the wait never depends on its socket.
_FANOUT_CHUNK = 100
_FANOUT_BREATH = 0.02
_REPLAY_LIMIT = 500
_BUFFER = 5000


def _csv(v: Optional[str]) -> set[str]:
    if not v:
        return set()
    return {x.strip() for x in str(v).split(",") if x.strip()}


def _num(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def sanitize(obj: Any) -> Any:
    """JSON-safe copy (NaN / inf -> None, numpy scalars -> python)."""
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if hasattr(obj, "item"):
        try:
            return sanitize(obj.item())
        except Exception:
            return None
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    return obj


@dataclass(frozen=True)
class Subscription:
    """What a client wants. Empty set = no restriction."""
    sources: frozenset[str] = frozenset()
    setups: frozenset[str] = frozenset()
    triggers: frozenset[str] = frozenset()
    symbols: frozenset[str] = frozenset()
    direction: Optional[str] = None
    min_score: Optional[float] = None
    custom: Optional[bool] = None          # True = only custom, False = exclude custom

    @classmethod
    def from_params(cls, params: Any) -> "Subscription":
        g = (lambda k: params.get(k)) if hasattr(params, "get") else (lambda k: None)
        sources = {s.lower() for s in _csv(g("sources"))}
        if "all" in sources or "*" in sources:
            sources = set()
        direction = (g("direction") or "").strip().lower() or None
        if direction not in (None, "long", "short", "neutral"):
            direction = None
        custom_raw = (g("custom") or "").strip().lower()
        custom = True if custom_raw in ("1", "true", "yes", "only") else False if custom_raw in ("0", "false", "no") else None
        return cls(
            sources=frozenset(sources),
            setups=frozenset(_csv(g("setups"))),
            triggers=frozenset(_csv(g("triggers"))),
            symbols=frozenset(s.upper() for s in _csv(g("symbols"))),
            direction=direction,
            min_score=_num(g("min_score")),
            custom=custom,
        )

    def matches(self, a: dict) -> bool:
        if self.sources and a.get("source") not in self.sources:
            return False
        if self.custom is True and not a.get("custom"):
            return False
        if self.custom is False and a.get("custom"):
            return False
        if self.setups and str(a.get("setup") or "") not in self.setups:
            return False
        if self.triggers and not ({str(a.get("trigger") or ""), str(a.get("entry_trigger") or "")} & self.triggers):
            return False
        if self.symbols and str(a.get("symbol") or "").upper() not in self.symbols:
            return False
        if self.direction and a.get("direction") != self.direction:
            return False
        if self.min_score is not None and _num(a.get("score")) is not None and _num(a.get("score")) < self.min_score:
            return False
        return True

    def describe(self) -> dict:
        return {
            "sources": sorted(self.sources) or "all", "setups": sorted(self.setups) or "all",
            "triggers": sorted(self.triggers) or "all", "symbols": sorted(self.symbols) or "all",
            "direction": self.direction or "all", "min_score": self.min_score, "custom": self.custom,
        }


@dataclass
class _Client:
    send: Any                       # async callable(str)
    sub: Subscription
    name: str = ""
    since: int = 0                  # alerts with seq <= since were in the replay already
    sent: int = 0
    connected_at: float = field(default_factory=lambda: __import__("time").time())
    close: Any = None               # async callable() that closes the socket, optional
    outbox: Any = None              # asyncio.Queue[str]; the ONLY path to the socket
    task: Any = None                # the sender task draining `outbox`


class _TapSink(AlertSink):
    """AlertSink wrapper: forwards to the legacy sink, publishes what it accepts."""

    def __init__(self, inner: AlertSink, hub: "FeedHub", source: str) -> None:
        self._inner = inner
        self._hub = hub
        self._source = source

    def push(self, alert: dict) -> bool:
        accepted = self._inner.push(alert)
        if accepted:
            self._hub.publish(alert, self._source)
        return accepted

    # the LiveScanner reads / clears its sinks; delegate everything else
    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def clear(self) -> None:
        self._inner.clear()

    def all(self) -> list[dict]:
        return self._inner.all()

    def __len__(self) -> int:
        return len(self._inner)


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class FeedHub:
    """One bus for every alert, one WebSocket contract for every consumer."""

    def __init__(self, store_dir: Path = Path("data/alerts/all"), keep_days: int = 5,
                 buffer: int = _BUFFER, load_persisted: bool = False) -> None:
        self.store = AlertStore(store_dir=store_dir, keep_days=keep_days)
        self.recent: deque[dict] = deque(maxlen=buffer)
        self._queue: queue.Queue[dict] = queue.Queue()
        self._clients: dict[int, _Client] = {}
        self._lock = threading.Lock()
        self._next_id = 1
        self.published = 0
        self.dropped_clients = 0           # disconnected for being too slow
        self._closing: set = set()         # close handshakes in flight (kept referenced)
        if load_persisted:
            for a in self.store.load_recent():
                self.recent.append(a)

    # ── producers ──
    def tap(self, sink: AlertSink, source: str) -> AlertSink:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        sink.restore_cooldowns([
            alert for alert in self.recent if alert.get("source") == source
        ])
        return _TapSink(sink, self, source)

    def publish(self, alert: dict, source: str) -> dict:
        a = sanitize(dict(alert))
        a["source"] = "custom" if a.get("custom") else source
        with self._lock:
            self.published += 1
            a["seq"] = self.published          # monotonic per process; clients dedupe on it
            self.recent.append(a)
        self.store.save(a)
        self._queue.put(a)
        return a

    # ── consumers ──
    def recent_for(self, sub: Subscription, limit: int = 500) -> list[dict]:
        with self._lock:
            items = list(self.recent)
        out = [a for a in reversed(items) if sub.matches(a)]
        return out[:limit]

    def add_client(self, send, sub: Subscription, name: str = "", close=None, replay: bool = False) -> int:
        """Register a subscriber. With `replay`, the replay frame is queued first.

        The high-water mark (`since`) and the replay snapshot are taken under the
        same lock publish() holds while it assigns `seq` and appends to `recent`,
        so every alert is in exactly one of the two: the replay, or the live
        stream. The replay goes through the outbox like everything else, so one
        sender owns every write to the socket and frames cannot interleave.
        """
        with self._lock:
            cid = self._next_id
            self._next_id += 1
            c = _Client(send=send, sub=sub, name=name, since=self.published, close=close,
                        outbox=asyncio.Queue(maxsize=_CLIENT_QUEUE))
            if replay:
                matching = [a for a in reversed(self.recent) if sub.matches(a)]
                c.outbox.put_nowait(json.dumps({
                    "type": "replay", "alerts": matching[:_REPLAY_LIMIT], "filter": sub.describe(),
                    # True when older matching alerts did not fit: the client has a
                    # gap it can only fill from /api/alerts or the archive.
                    "truncated": len(matching) > _REPLAY_LIMIT,
                }))
            self._clients[cid] = c
        return cid

    def _ensure_sender(self, cid: int, c: _Client) -> None:
        if c.task is None:
            c.task = asyncio.create_task(self._sender(cid, c))

    def remove_client(self, cid: int) -> None:
        with self._lock:
            c = self._clients.pop(cid, None)
        if c is not None and c.task is not None and c.task is not _current_task():
            c.task.cancel()

    def clients(self) -> list[dict]:
        with self._lock:
            return [{"id": cid, "name": c.name, "sent": c.sent, "filter": c.sub.describe(),
                     "queued": c.outbox.qsize() if c.outbox is not None else 0}
                    for cid, c in self._clients.items()]

    def clear(self) -> None:
        with self._lock:
            self.recent.clear()

    async def broadcast_loop(self) -> None:
        """Poll the queue every 50 ms and fan out to every subscriber whose filter matches."""
        while True:
            await asyncio.sleep(_POLL)
            batch: list[dict] = []
            try:
                while True:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            if not batch:
                continue
            with self._lock:
                clients = list(self._clients.items())
            for n, a in enumerate(batch, 1):
                msg = None
                for cid, c in clients:
                    if int(a.get("seq") or 0) <= c.since or not c.sub.matches(a):
                        continue          # already in that client's replay, or filtered out
                    if msg is None:
                        msg = json.dumps({"type": "alert", "alert": a})
                    with self._lock:
                        if cid not in self._clients:
                            continue      # dropped earlier in this batch
                    self._ensure_sender(cid, c)
                    try:
                        c.outbox.put_nowait(msg)      # never awaits a socket
                    except asyncio.QueueFull:
                        self._drop(cid, c, "outbox full")
                if n % _FANOUT_CHUNK == 0:
                    await asyncio.sleep(0)            # let the senders drain
                    if any(c.outbox.qsize() > _CLIENT_QUEUE // 2 for _, c in clients
                           if c.task is not None and not c.task.done()):
                        await asyncio.sleep(_FANOUT_BREATH)

    async def _sender(self, cid: int, c: _Client) -> None:
        """Drain one client's outbox in order, with a deadline on every send."""
        try:
            while True:
                msg = await c.outbox.get()
                await asyncio.wait_for(c.send(msg), _SEND_TIMEOUT)
                c.sent += 1
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._drop(cid, c, f"send took over {_SEND_TIMEOUT:g}s")
        except Exception:
            self.remove_client(cid)               # socket already gone

    def _drop(self, cid: int, c: _Client, why: str) -> None:
        """Disconnect a client that is behind. Never awaits: the close handshake of
        a stalled socket runs in its own task, off the fan-out loop."""
        with self._lock:
            if cid not in self._clients:
                return
        self.dropped_clients += 1
        log.warning("feed: dropping slow client %d %s (%s); it can reconnect and replay", cid, c.name, why)
        self.remove_client(cid)
        if c.close is not None:
            async def _close() -> None:
                try:
                    await asyncio.wait_for(c.close(), 1.0)
                except Exception:
                    pass
            t = asyncio.create_task(_close())
            self._closing.add(t)
            t.add_done_callback(self._closing.discard)

    async def serve(self, ws, params: Any, name: str = "") -> None:
        """Run one WebSocket client until it disconnects (FastAPI / Starlette)."""
        from starlette.websockets import WebSocketDisconnect
        sub = Subscription.from_params(params)
        await ws.accept()
        cid = self.add_client(ws.send_text, sub, name or (ws.client.host if ws.client else ""),
                              close=lambda: ws.close(code=1013), replay=True)
        log.info("feed: client %d connected %s (%d total)", cid, sub.describe(), len(self._clients))
        try:
            self._ensure_sender(cid, self._clients[cid])      # sends the queued replay first
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("feed: client %d error: %s", cid, exc)
        finally:
            self.remove_client(cid)
            log.info("feed: client %d disconnected (%d remaining)", cid, len(self._clients))
