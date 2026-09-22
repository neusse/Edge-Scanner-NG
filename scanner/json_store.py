"""Atomic JSON persistence for Dashboard V2 (layouts + watchlists).

Pattern follows scanner/feedback_store.py: small files, whole-file rewrite on
every save, tolerant reads. Writes go to a temp file then `os.replace` so a
crash mid-write never leaves a half-written JSON on disk (Windows-safe).

`data/` is gitignored, so nothing persisted here is ever committed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def sanitize_id(raw: object) -> Optional[str]:
    """Return `raw` if it is a safe path-component id, else None."""
    if not isinstance(raw, str):
        return None
    return raw if _ID_RE.match(raw) else None


class AtomicJsonStore:
    """One JSON document on disk, written atomically under a lock."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> Any | None:
        """Return the parsed document, or None if missing/unparsable."""
        with self._lock:
            return _read_json(self._path)

    def write(self, obj: Any) -> None:
        with self._lock:
            _write_json_atomic(self._path, obj)


class LayoutStore:
    """One file per screen: data/layouts/<id>.json."""

    def __init__(self, dir: Path = Path("data/layouts")) -> None:
        self._dir = Path(dir)
        self._lock = threading.Lock()
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        return self._dir

    def load_all(self) -> list[dict]:
        """All screens, sorted by file name; unparsable files are skipped."""
        out: list[dict] = []
        with self._lock:
            for p in sorted(self._dir.glob("*.json"), key=lambda q: q.name):
                doc = _read_json(p)
                if isinstance(doc, dict):
                    out.append(doc)
                else:
                    log.warning("LayoutStore: skipping unparsable %s", p.name)
        return out

    def save(self, screen: dict) -> str:
        """Write the screen to <id>.json. Returns the id. Raises ValueError on a bad id."""
        sid = sanitize_id(screen.get("id") if isinstance(screen, dict) else None)
        if sid is None:
            raise ValueError("screen['id'] must match [A-Za-z0-9_-]{1,64}")
        with self._lock:
            _write_json_atomic(self._dir / f"{sid}.json", screen)
        return sid

    def delete(self, id: str) -> bool:
        sid = sanitize_id(id)
        if sid is None:
            return False
        with self._lock:
            p = self._dir / f"{sid}.json"
            if not p.exists():
                return False
            try:
                p.unlink()
            except OSError as exc:
                log.warning("LayoutStore: delete error %s: %s", p.name, exc)
                return False
        return True


class WatchlistStore:
    """All watchlists in one file: data/watchlists.json -> {"watchlists": [...]}."""

    def __init__(self, path: Path = Path("data/watchlists.json")) -> None:
        self._store = AtomicJsonStore(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._store.path

    def load_all(self) -> list[dict]:
        doc = self._store.read()
        if isinstance(doc, dict):
            lst = doc.get("watchlists")
        elif isinstance(doc, list):
            lst = doc
        else:
            lst = None
        return [w for w in (lst or []) if isinstance(w, dict)]

    def save(self, wl: dict) -> dict:
        """Upsert by id. Symbols are uppercased and deduped (order preserved).

        Optional descriptive and provenance fields stay deliberately small so
        the file remains comfortable to inspect and edit by hand.
        """
        wid = sanitize_id(wl.get("id") if isinstance(wl, dict) else None)
        if wid is None:
            raise ValueError("watchlist['id'] must match [A-Za-z0-9_-]{1,64}")
        prev = next((w for w in self.load_all() if w.get("id") == wid), None)
        rec = {
            "id": wid,
            "name": str(wl.get("name") or wid).strip()[:80] or wid,
            "description": str(wl.get("description") or "").strip()[:800],
            "symbols": normalize_symbols(wl.get("symbols")),
            "createdAt": str(wl.get("createdAt") or (prev or {}).get("createdAt") or wl.get("updatedAt") or ""),
            "updatedAt": str(wl.get("updatedAt") or ""),
        }
        for key, limit in (("source", 32), ("sourceLabel", 120), ("sourceProfileId", 64),
                           ("sourceProfileHash", 40), ("capturedAt", 64)):
            value = str(wl.get(key) or "").strip()[:limit]
            if value:
                rec[key] = value
        with self._lock:
            lists = self.load_all()
            for i, w in enumerate(lists):
                if w.get("id") == wid:
                    lists[i] = rec
                    break
            else:
                lists.append(rec)
            self._store.write({"watchlists": lists})
        return rec

    def delete(self, id: str) -> bool:
        wid = sanitize_id(id)
        if wid is None:
            return False
        with self._lock:
            lists = self.load_all()
            remaining = [w for w in lists if w.get("id") != wid]
            if len(remaining) == len(lists):
                return False
            self._store.write({"watchlists": remaining})
        return True


class UniverseSelectionStore:
    """The one watchlist chosen for the next scanner start.

    Kept separate from watchlists so a list remains an ordinary reusable list
    and assigning it is an explicit, auditable action.
    """

    def __init__(self, path: Path = Path("data/universe_selection.json")) -> None:
        self._store = AtomicJsonStore(path)

    @property
    def path(self) -> Path:
        return self._store.path

    def load(self) -> Optional[str]:
        doc = self._store.read()
        if not isinstance(doc, dict):
            return None
        return sanitize_id(doc.get("watchlist_id"))

    def save(self, watchlist_id: Optional[str]) -> Optional[str]:
        if watchlist_id in (None, ""):
            self._store.write({"watchlist_id": None})
            return None
        wid = sanitize_id(watchlist_id)
        if wid is None:
            raise ValueError("watchlist_id must match [A-Za-z0-9_-]{1,64}")
        self._store.write({"watchlist_id": wid})
        return wid


# ── helpers ──────────────────────────────────────────────────────────────────

def normalize_symbols(symbols: object) -> list[str]:
    """Uppercase, strip, drop blanks, dedupe preserving first occurrence."""
    if not isinstance(symbols, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        if not isinstance(s, str):
            continue
        u = s.strip().upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("json_store: read error %s: %s", path, exc)
        return None


def _write_json_atomic(path: Path, obj: Any) -> None:
    """Write via a sibling temp file + os.replace (atomic on Windows and POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("json_store: write error %s: %s", path, exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
