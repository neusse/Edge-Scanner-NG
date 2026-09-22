import json
import sqlite3
from datetime import datetime, timezone

from scripts.import_schwab_py_token import import_token


def _write_token(path, created, access="access", refresh="refresh"):
    path.write_text(json.dumps({
        "creation_timestamp": created,
        "token": {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": "id",
            "expires_in": 1800,
            "expires_at": created + 1800,
            "token_type": "Bearer",
            "scope": "api",
        },
    }), encoding="utf-8")


def test_imports_schwab_py_json_into_schwabdev_database(tmp_path):
    source, destination = tmp_path / "token.json", tmp_path / "tokens.db"
    created = datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp()
    _write_token(source, created)

    assert import_token(source, destination) is True
    with sqlite3.connect(destination) as con:
        row = con.execute(
            "SELECT access_token, refresh_token, refresh_token_issued FROM schwabdev"
        ).fetchone()
    assert row[:2] == ("access", "refresh")
    assert datetime.fromisoformat(row[2]).timestamp() == created


def test_does_not_overwrite_a_newer_schwabdev_token(tmp_path):
    source, destination = tmp_path / "token.json", tmp_path / "tokens.db"
    created = datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp()
    _write_token(source, created, access="old-access")
    assert import_token(source, destination) is True

    with sqlite3.connect(destination) as con:
        newer = datetime(2026, 9, 22, tzinfo=timezone.utc).isoformat()
        con.execute(
            "UPDATE schwabdev SET access_token = ?, refresh_token_issued = ?",
            ("new-access", newer),
        )
        con.commit()

    assert import_token(source, destination) is False
    with sqlite3.connect(destination) as con:
        assert con.execute("SELECT access_token FROM schwabdev").fetchone()[0] == "new-access"
