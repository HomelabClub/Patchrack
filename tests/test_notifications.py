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
