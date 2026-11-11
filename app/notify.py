# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Push notifications through Pushover and Prowl."""
from __future__ import annotations

import json
import logging
import time
import xml.etree.ElementTree as ET

import httpx

from . import db

log = logging.getLogger("patchrack.notify")


async def _pushover(title: str, message: str, priority: int) -> tuple[bool, str]:
    token, user = db.get_setting("pushover_token"), db.get_setting("pushover_user")
    if not token or not user:
        return False, "Pushover needs an application token and a user key"
    data = {"token": token, "user": user, "title": title, "message": message[:1024],
            "priority": str(priority)}
    url = db.get_setting("app_url")
    if url:
        data.update({"url": url, "url_title": "Open Patchrack"})
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post("https://api.pushover.net/1/messages.json", data=data)
    if r.status_code == 200 and r.json().get("status") == 1:
        return True, "Sent"
    try:
        errs = ", ".join(r.json().get("errors", [])) or r.text[:200]
    except ValueError:
        errs = r.text[:200]
    return False, f"Pushover rejected the message: {errs}"


async def _prowl(title: str, message: str, priority: int) -> tuple[bool, str]:
    key = db.get_setting("prowl_apikey")
    if not key:
        return False, "Prowl needs an API key"
    data = {"apikey": key, "application": "Patchrack", "event": title,
            "description": message[:10000], "priority": str(max(-2, min(2, priority)))}
    url = db.get_setting("app_url")
    if url:
        data["url"] = url
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post("https://api.prowlapp.com/publicapi/add", data=data)
    try:
        root = ET.fromstring(r.text)
        node = root.find("success")
        if node is not None:
            return True, "Sent"
        err = root.find("error")
        return False, f"Prowl rejected the message: {err.text if err is not None else r.text[:200]}"
    except ET.ParseError:
        return False, f"Unexpected Prowl response (HTTP {r.status_code})"


CHANNELS = {"pushover": _pushover, "prowl": _prowl}


async def send(title: str, message: str, priority: int = 0, only: str | None = None) -> dict[str, tuple[bool, str]]:
    results = {}
    for name, fn in CHANNELS.items():
        if only and name != only:
            continue
        if not only and not db.get_setting(f"{name}_enabled"):
            continue
        try:
            results[name] = await fn(title, message, priority)
        except Exception as exc:  # network errors must never break a job
            results[name] = (False, f"Could not reach {name}: {exc}")
        if not results[name][0]:
            log.warning("%s notification failed: %s", name, results[name][1])
    return results


LABELS = {"pushover": "Pushover", "prowl": "Prowl"}


def enabled_channels() -> list[str]:
    return [name for name in CHANNELS if db.get_setting(f"{name}_enabled")]


def record(kind: str, title: str, message: str, results: dict | None, note: str | None = None) -> None:
    """Keep the last 100 notifications, so Settings can show what was sent and why not."""
    db.ex("INSERT INTO notifications(at, kind, title, message, results, note) VALUES(?,?,?,?,?,?)",
          (time.time(), kind, title, message, json.dumps(results or {}), note))
    db.ex("DELETE FROM notifications WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 100)")


async def event(kind: str, title: str, message: str, priority: int = 0) -> None:
    """Send if the user enabled this kind of event, and log the outcome either way."""
    if not db.get_setting(f"notify_{kind}"):
        record(kind, title, message, None, "Not sent: this kind of message is turned off in Settings.")
        return
    results = await send(title, message, priority)
    note = None if results else "Not sent: Pushover and Prowl are both switched off."
    record(kind, title, message, results, note)
