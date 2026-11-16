# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""When does Patchrack send a message, and is every attempt logged?"""
import asyncio
import time

import pytest

from app import db, jobs, notify


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_event(kind, title, message, priority=0):
        calls.append((kind, title, message))

    monkeypatch.setattr(jobs.notify, "event", fake_event)
    db.set_setting("last_notified_updates_found", 0)
    yield calls


def announce(is_new, lines=("jellyserver: 23 updates, 4 security",)):
    asyncio.run(jobs._announce("updates_found", list(lines), is_new, "Updates available", "Updates still waiting"))


@pytest.mark.parametrize("mode", ["daily", "every", "never"])
def test_new_updates_are_always_reported(sent, mode):
    db.set_setting("notify_reminder", mode)
    announce(is_new=True)
    assert sent == [("updates_found", "Updates available", "jellyserver: 23 updates, 4 security")]


def test_known_updates_get_a_daily_reminder(sent):
    db.set_setting("notify_reminder", "daily")
    announce(is_new=False)                       # never reminded before
    assert [t for _, t, _ in sent] == ["Updates still waiting"]
    announce(is_new=False)                       # a few hours later: no repeat
    assert len(sent) == 1
    db.set_setting("last_notified_updates_found", time.time() - 21 * 3600)
    announce(is_new=False)                       # next day
    assert len(sent) == 2


def test_every_check_mode_repeats(sent):
    db.set_setting("notify_reminder", "every")
    announce(is_new=False)
    announce(is_new=False)
    assert len(sent) == 2


def test_never_mode_stays_quiet_for_known_updates(sent):
    db.set_setting("notify_reminder", "never")
    announce(is_new=False)
    assert sent == []


def test_nothing_waiting_sends_nothing(sent):
    db.set_setting("notify_reminder", "every")
    announce(is_new=False, lines=())
    assert sent == []


def _last_log():
    return db.q1("SELECT * FROM notifications ORDER BY id DESC LIMIT 1")


def test_message_with_all_services_off_is_logged_with_reason():
    db.set_setting("notify_updates_found", True)
    db.set_setting("pushover_enabled", False)
    db.set_setting("prowl_enabled", False)
    asyncio.run(notify.event("updates_found", "Updates available", "nas: 2 updates"))
    row = _last_log()
    assert row["title"] == "Updates available"
    assert "switched off" in row["note"]


def test_message_type_turned_off_is_logged_with_reason():
    db.set_setting("notify_unreachable", False)
    asyncio.run(notify.event("unreachable", "Servers unreachable", "nas"))
    assert "turned off" in _last_log()["note"]
    db.set_setting("notify_unreachable", True)


def test_sent_message_is_logged_with_each_result(monkeypatch):
    async def ok(title, message, priority):
        return True, "Sent"

    async def bad(title, message, priority):
        return False, "invalid API key"

    monkeypatch.setitem(notify.CHANNELS, "pushover", ok)
    monkeypatch.setitem(notify.CHANNELS, "prowl", bad)
    db.set_setting("pushover_enabled", True)
    db.set_setting("prowl_enabled", True)
    asyncio.run(notify.event("updates_found", "Updates available", "nas: 2 updates"))
    row = _last_log()
    assert row["note"] is None
    assert '"pushover": [true, "Sent"]' in row["results"] and "invalid API key" in row["results"]


# ---- automatic updates: each finished run is reported once ----------------------------
def _auto_server():
    sid = db.ex("INSERT INTO servers(name,host,username,created_at,auto_enabled) VALUES('autobox','10.9.9.9','u',?,1)",
                (time.time(),))
    return sid


def _facts(last: str):
    f = jobs.Facts()
    f.auto_helper = "1"
    f.auto_status = "enabled=1;conf_hour=3;" + last + ";tz=CET +0100;cron=1"
    return f


def test_auto_update_runs_are_reported_once(sent):
    sid = _auto_server()
    job = jobs.Job(id="t1", server_id=sid, server_name="autobox", kind="check", trigger="schedule", options={})
    ok = "state=success;packages=3;containers=web;reboot=1;finished=100"
    asyncio.run(jobs.manager._store_auto(job, _facts(ok)))
    assert sent == [("update_done", "autobox: automatic update finished",
                     "Installed 3 updates; updated container web; rebooted the server.")]
    asyncio.run(jobs.manager._store_auto(job, _facts(ok)))           # same run, next check
    assert len(sent) == 1
    asyncio.run(jobs.manager._store_auto(job, _facts("state=success;packages=0;containers=;reboot=0;finished=200")))
    assert len(sent) == 1                                             # nothing changed: stay quiet
    asyncio.run(jobs.manager._store_auto(job, _facts("state=failed;exit=1;packages=2;finished=300")))
    assert sent[-1][0] == "update_failed" and "failed" in sent[-1][2]
    db.ex("DELETE FROM servers WHERE id=?", (sid,))


def test_auto_update_runs_are_not_reported_when_off(sent):
    sid = _auto_server()
    db.ex("UPDATE servers SET auto_enabled=0 WHERE id=?", (sid,))
    job = jobs.Job(id="t2", server_id=sid, server_name="autobox", kind="check", trigger="schedule", options={})
    asyncio.run(jobs.manager._store_auto(job, _facts("state=failed;exit=1;finished=1")))
    assert sent == []
    db.ex("DELETE FROM servers WHERE id=?", (sid,))


def test_containers_updated_by_a_run_no_longer_show_as_waiting(sent):
    import json
    sid = _auto_server()
    db.ex("UPDATE servers SET docker_updates=2, docker_json=? WHERE id=?", (json.dumps({"updates": 2, "containers": [
        {"name": "web", "check": "update"}, {"name": "plain", "check": "update"}, {"name": "db", "check": "current"}]}), sid))
    job = jobs.Job(id="t3", server_id=sid, server_name="autobox", kind="check", trigger="schedule", options={})
    asyncio.run(jobs.manager._store_auto(job, _facts("state=success;packages=0;containers=web;reboot=0;finished=5")))
    info = json.loads(db.q1("SELECT docker_json FROM servers WHERE id=?", (sid,))["docker_json"])
    assert [c["check"] for c in info["containers"]] == ["current", "update", "current"]   # 'plain' isn't from Compose
    assert db.q1("SELECT docker_updates FROM servers WHERE id=?", (sid,))["docker_updates"] == 1
    db.ex("DELETE FROM servers WHERE id=?", (sid,))
