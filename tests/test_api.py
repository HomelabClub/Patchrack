# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""API tests against the real app with a throwaway database (no servers are contacted)."""
import subprocess
import time

import asyncssh
import pytest
from fastapi.testclient import TestClient

from app import db, main

H = {"x-patchrack": "1"}
PASSWORD = "a-long-test-password"


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def test_first_run_requires_setup(client):
    assert client.get("/api/session").json()["setup_required"] is True
    assert client.get("/api/servers").status_code == 401


def test_state_changes_need_the_csrf_header(client):
    r = client.post("/api/setup", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 403


def test_setup_rejects_short_password(client):
    r = client.post("/api/setup", json={"username": "admin", "password": "short"}, headers=H)
    assert r.status_code == 400


def test_setup_creates_account_and_signs_in(client):
    r = client.post("/api/setup", json={"username": "admin", "password": PASSWORD}, headers=H)
    assert r.status_code == 200
    s = client.get("/api/session").json()
    assert s["user"] == "admin" and s["license"] == "AGPL-3.0-or-later"
    assert client.post("/api/setup", json={"username": "x", "password": PASSWORD}, headers=H).status_code == 409


def test_server_validation(client):
    bad = {"name": "x", "host": "bad host; rm -rf /", "username": "patchrack"}
    assert client.post("/api/servers", json=bad, headers=H).status_code == 400
    bad_user = {"name": "x", "host": "10.0.0.5", "username": "Root Admin"}
    assert client.post("/api/servers", json=bad_user, headers=H).status_code == 400
    bad_key = {"name": "x", "host": "10.0.0.5", "key_source": "custom", "private_key": "not a key"}
    assert client.post("/api/servers", json=bad_key, headers=H).status_code == 400


def test_secrets_are_never_returned(client):
    key = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    body = {"name": "nas", "host": "10.0.0.40", "username": "patchrack", "key_source": "custom",
            "private_key": key, "become": "sudo_password", "sudo_password": "hunter2-sudo"}
    r = client.post("/api/servers", json=body, headers=H)
    assert r.status_code == 200
    text = client.get("/api/servers").text
    assert "PRIVATE KEY" not in text and "hunter2-sudo" not in text and "_enc" not in text
    srv = r.json()
    assert srv["has_custom_key"] and srv["has_sudo_password"]
    row = db.q1("SELECT private_key_enc, sudo_password_enc FROM servers WHERE id=?", (srv["id"],))
    assert row["private_key_enc"].startswith("v1:") and "PRIVATE" not in row["private_key_enc"]


def test_editing_keeps_stored_secrets_when_fields_are_empty(client):
    sid = client.get("/api/servers").json()[0]["id"]
    body = {"name": "nas-renamed", "host": "10.0.0.40", "username": "patchrack", "key_source": "custom",
            "become": "sudo_password"}
    r = client.put(f"/api/servers/{sid}", json=body, headers=H)
    assert r.status_code == 200
    assert r.json()["has_custom_key"] and r.json()["has_sudo_password"]


def test_notification_tokens_are_masked(client):
    s = client.get("/api/settings").json()
    s.update(pushover_enabled=True, pushover_token="tok-123456", pushover_user="usr-abcdef")
    r = client.put("/api/settings", json=s, headers=H)
    assert r.status_code == 200
    text = client.get("/api/settings").text
    assert "tok-123456" not in text and "usr-abcdef" not in text
    assert r.json()["has_pushover_token"] is True


def test_send_test_warns_when_the_service_is_switched_off(client, monkeypatch):
    from app import notify

    async def ok(title, message, priority):
        return True, "Sent"

    monkeypatch.setitem(notify.CHANNELS, "prowl", ok)
    s = client.get("/api/settings").json()
    s.update(prowl_enabled=False, prowl_apikey="key-123")
    client.put("/api/settings", json=s, headers=H)
    r = client.post("/api/notify/test", json={"channel": "prowl"}, headers=H).json()
    assert r["ok"] is True and r["enabled"] is False
    log = client.get("/api/notifications").json()
    assert log[0]["title"] == "Patchrack test" and log[0]["results"]["prowl"][0] is True


def test_settings_show_next_check_and_reminder(client):
    s = client.get("/api/settings").json()
    assert s["notify_reminder"] == "daily"
    assert s["next_scheduled_check"] >= time.time() - 5
    s["notify_reminder"] = "sometimes"
    assert client.put("/api/settings", json=s, headers=H).status_code == 422


def test_setup_script_is_valid_and_restricted(client):
    k = client.get("/api/ssh-key?username=patchrack").json()
    script = k["setup_script"]
    assert k["public_key"].startswith("ssh-ed25519 ")
    assert k["public_key"] in script
    assert "visudo -cf" in script and "NOPASSWD: $APT, $DPKG --configure -a, $SCTL reboot" in script
    # the heredoc body must be valid bash
    assert script.startswith("bash -s <<'PATCHRACK'\n") and script.endswith("\nPATCHRACK")
    body = script.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert subprocess.run(["bash", "-n"], input=body, text=True).returncode == 0
    assert client.get("/api/ssh-key?username=bad;user").status_code == 400


def test_docker_actions_need_a_checked_docker_host(client):
    sid = client.get("/api/servers").json()[0]["id"]
    r = client.post(f"/api/servers/{sid}/docker/update", json={"container_ids": ["a" * 64]}, headers=H)
    assert r.status_code == 409


def test_failed_logins_are_rate_limited(client):
    client.post("/api/logout", headers=H)
    for _ in range(5):
        assert client.post("/api/login", json={"username": "admin", "password": "wrong"}, headers=H).status_code == 401
    r = client.post("/api/login", json={"username": "admin", "password": PASSWORD}, headers=H)
    assert r.status_code == 429
