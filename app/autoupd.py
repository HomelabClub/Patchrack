# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Automatic updates that run on the server itself, from a cron job.

Patchrack installs /usr/local/sbin/patchrack-auto-update with the setup command, turns the
cron job on and off through it, and reads the result of each run. While automatic updates
are on, Patchrack's own scheduled checks only read results for that server; they don't
refresh package lists or look for container or release updates there.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

PATH = "/usr/local/sbin/patchrack-auto-update"
VERSION = 1
SOURCE = (Path(__file__).parent / "auto_update.sh").read_text()
DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def parse_status(text: str) -> dict:
    """The output of `patchrack-auto-update status` (lines or ;-joined) as a dict."""
    raw: dict[str, str] = {}
    for part in text.replace(";", "\n").splitlines():
        if "=" in part:
            k, v = part.split("=", 1)
            raw[k.strip()] = v.strip()
    if not raw:
        return {}
    conf = {k[5:]: v for k, v in raw.items() if k.startswith("conf_")}
    state = {k: v for k, v in raw.items()
             if not k.startswith("conf_") and k not in ("enabled", "tz", "cron")}
    return {"enabled": raw.get("enabled") == "1", "conf": conf, "last": state,
            "tz": raw.get("tz", ""), "cron": raw.get("cron") == "1", "read_at": time.time()}


def cron_days(days: list[int]) -> str:
    days = sorted(set(days))
    return "*" if not days or len(days) == 7 else ",".join(str(d) for d in days)


def describe_schedule(cfg: dict) -> str:
    days = cfg.get("days") or []
    when = "every day" if not days or len(days) == 7 else "on " + ", ".join(
        DAY_NAMES[d] for d in sorted(days, key=lambda d: (d + 6) % 7))
    return f"{when} at {cfg['hour']:02d}:{cfg['minute']:02d}"


def run_key(last: dict) -> str:
    """Identifies one finished run, so it's reported only once."""
    return f"{last.get('state')}:{last.get('finished', '')}"


def describe_run(last: dict) -> str:
    st = last.get("state")
    if st == "skipped":
        return f"The automatic update was skipped: {last.get('reason', 'unknown reason')}."
    if st == "failed":
        return "The automatic update failed. Open the server and choose Show log for the details."
    n = int(last.get("packages") or 0)
    parts = [f"Installed {n} update{'' if n == 1 else 's'}" if n else "No system updates"]
    names = (last.get("containers") or "").split()
    if names:
        parts.append(f"updated container{'' if len(names) == 1 else 's'} {', '.join(names)}")
    text = "; ".join(parts)
    if st == "partial":
        text += "; updating some containers failed"
    if last.get("reboot") == "1":
        text += "; rebooted the server"
    return text + "."


def public(row) -> dict:
    """What the GUI gets about automatic updates for one server."""
    status = json.loads(row["auto_state"]) if row["auto_state"] else {}
    cfg = json.loads(row["auto_config"]) if row["auto_config"] else None
    last = status.get("last") or {}
    return {
        "enabled": bool(row["auto_enabled"]),
        "config": cfg,
        "schedule": describe_schedule(cfg) if cfg else None,
        "tz": status.get("tz", ""),
        "helper": status.get("helper"),
        "helper_ok": bool(status.get("helper")) and int(status.get("helper") or 0) >= VERSION,
        "server_enabled": status.get("enabled"),       # what the server itself reports
        "last": last if last.get("state") not in (None, "none") else None,
        "last_text": describe_run(last) if last.get("state") not in (None, "none", "running") else None,
        "read_at": status.get("read_at"),
        "enabled_at": float((status.get("conf") or {}).get("enabled_at") or 0) or None,
    }
