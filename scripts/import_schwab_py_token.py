#!/usr/bin/env python
"""Synchronize an existing schwab-py JSON token into schwabdev's database.

The scanner remains a pure schwabdev client.  This helper only translates the
on-disk token at startup, without replacing a newer schwabdev access token with
an expired shared copy. It never opens a browser, requests a token, or starts a stream.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def import_token(source: Path, destination: Path) -> bool:
    """Import a newer login or fresher same-login access token; never roll back."""
    payload = json.loads(source.read_text(encoding="utf-8"))
    token = payload.get("token") or {}
    created = float(payload["creation_timestamp"])
    required = ("access_token", "refresh_token")
    missing = [name for name in required if not token.get(name)]
    if missing:
        raise ValueError(f"shared token is missing: {', '.join(missing)}")

    expires_in = int(token.get("expires_in") or 1800)
    expires_at = float(token.get("expires_at") or (created + expires_in))
    access_issued = expires_at - expires_in

    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS schwabdev (
                access_token_issued TEXT NOT NULL,
                refresh_token_issued TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                id_token TEXT NOT NULL,
                expires_in INTEGER,
                token_type TEXT,
                scope TEXT
            )
        """)
        row = con.execute(
            "SELECT access_token_issued, refresh_token_issued FROM schwabdev LIMIT 1"
        ).fetchone()
        if row:
            existing_access = datetime.fromisoformat(row[0])
            existing_login = datetime.fromisoformat(row[1])
            if existing_access.tzinfo is None:
                existing_access = existing_access.replace(tzinfo=timezone.utc)
            if existing_login.tzinfo is None:
                existing_login = existing_login.replace(tzinfo=timezone.utc)
            login_delta = created - existing_login.timestamp()
            if login_delta < -0.001:  # the shared file belongs to an older login
                return False
            if abs(login_delta) <= 0.001:
                if expires_at <= datetime.now(timezone.utc).timestamp() + 60:
                    return False  # do not import an expired/about-to-expire access token
                if access_issued <= existing_access.timestamp() + 0.001:
                    return False
                con.execute(
                    """UPDATE schwabdev SET access_token_issued = ?, access_token = ?,
                       id_token = ?, expires_in = ?, token_type = ?, scope = ?""",
                    (_utc_iso(access_issued), token["access_token"], token.get("id_token", ""),
                     expires_in, token.get("token_type", "Bearer"), token.get("scope", "api")),
                )
                con.commit()
                return True

        con.execute("DELETE FROM schwabdev")
        con.execute(
            """INSERT INTO schwabdev
               (access_token_issued, refresh_token_issued, access_token,
                refresh_token, id_token, expires_in, token_type, scope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _utc_iso(access_issued),
                _utc_iso(created),
                token["access_token"],
                token["refresh_token"],
                token.get("id_token", ""),
                expires_in,
                token.get("token_type", "Bearer"),
                token.get("scope", "api"),
            ),
        )
        con.commit()
    return True


def main() -> None:
    source_value = os.environ.get("SCHWAB_TOKEN_PATH")
    if not source_value:
        return
    source = Path(os.path.expandvars(source_value)).expanduser()
    destination = Path.home() / ".schwabdev" / "tokens.db"
    try:
        changed = import_token(source, destination)
    except Exception as exc:
        sys.exit(f"Could not import SCHWAB_TOKEN_PATH for schwabdev: {exc}")
    print("Schwab token: synchronized shared token" if changed
          else "Schwab token: schwabdev copy is current")


if __name__ == "__main__":
    main()
