"""Requests from foreign pages and rebound hostnames never reach write routes."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scanner import local_guard
from scanner.api import AppState, create_app
from scanner.api_v2 import _safe_static_path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCANNER_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(local_guard, "_extra_hosts", set())
    scanner = SimpleNamespace(_states={})
    return TestClient(create_app(AppState(scanner, None)), base_url="http://127.0.0.1:7777")


def test_foreign_origin_cannot_write(client):
    assert client.post("/api/v2/settings/reset", json={},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/v2/settings/reset", content=b"{}",
                       headers={"Origin": "null"}).status_code == 403


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_simple_post_without_json_content_type_is_refused(client, content_type):
    headers = {"Content-Type": content_type} if content_type else {}
    assert client.post("/api/v2/settings/reset", content=b"{}", headers=headers).status_code == 415


def test_rebound_host_is_refused_even_for_get(client):
    headers = {"Host": "rebound.evil.example:7777", "Origin": "http://rebound.evil.example:7777"}
    assert client.get("/api/v2/settings", headers=headers).status_code == 400
    assert client.post("/api/v2/settings/reset", json={}, headers=headers).status_code == 400


def test_local_scripts_and_explicit_lan_hosts_remain_allowed(client):
    assert client.post("/api/v2/settings/reset", json={}).status_code == 200
    headers = {"Host": "trading-pc:7777", "Origin": "http://trading-pc:7777"}
    assert client.get("/api/v2/settings", headers=headers).status_code == 400
    local_guard.allow_hosts("trading-pc")
    assert client.get("/api/v2/settings", headers=headers).status_code == 200


@pytest.mark.parametrize("path", ["//evil.example/share/a.js", "\\\\evil.example\\share\\a.js",
                                  "C:/Windows/win.ini", "assets/../../secret", "a\x00b"])
def test_static_paths_rejected_before_filesystem_resolution(path):
    assert not _safe_static_path(path)
