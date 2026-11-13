# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Detect whether a newer Ubuntu or Debian release is available, and whether upgrading is safe now.

Ubuntu: `do-release-upgrade -c` on the server, which follows the server's own setting in
/etc/update-manager/release-upgrades (LTS servers are offered the next LTS once its first
point release is out).
Debian: the current stable and oldstable releases are read from deb.debian.org; a server
is offered the next release only (Debian supports upgrading one release at a time).
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx

from .sshops import Facts, Target, run

log = logging.getLogger("patchrack.release")

HELPER_PATH = "/usr/local/sbin/patchrack-release-upgrade"
HELPER_VERSION = 1
HELPER_SOURCE = (Path(__file__).parent / "release_helper.sh").read_text()
MIN_FREE_BYTES = 5 * 1024 ** 3        # the same safety margin Ubuntu's own guides use
DEBIAN_RELEASES_URL = "https://deb.debian.org/debian/dists/{suite}/Release"
_debian_cache: dict = {"at": 0.0, "releases": {}}


async def debian_releases() -> dict[int, tuple[str, str]]:
    """{major: (codename, version)} for Debian stable and oldstable, cached for 12 hours."""
    if time.time() - _debian_cache["at"] < 12 * 3600 and _debian_cache["releases"]:
        return _debian_cache["releases"]
    found: dict[int, tuple[str, str]] = {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for suite in ("stable", "oldstable"):
            try:
                r = await client.get(DEBIAN_RELEASES_URL.format(suite=suite))
                if r.status_code != 200:
                    continue
                fields = dict(re.findall(r"^(Codename|Version): *(\S+)", r.text, re.M))
                major = int(fields.get("Version", "0").split(".")[0])
                if major and fields.get("Codename"):
                    found[major] = (fields["Codename"], fields["Version"])
            except (httpx.HTTPError, ValueError):
                continue
    if found:
        _debian_cache.update(at=time.time(), releases=found)
    return found or _debian_cache["releases"]


def parse_status(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


async def detect(conn, t: Target, f: Facts) -> dict:
    """What the Release upgrade section shows for this server. Never raises."""
    info = {"family": None, "current": f.os_name, "available": False, "target": None,
            "target_name": None, "unsupported": None, "note": None, "checked_at": time.time(),
            "helper": f.helper_version, "last": f.upgrade_state or None, "free_bytes": f.root_free}
    try:
        if f.os_id == "ubuntu":
            info["family"] = "ubuntu"
            code, out = await run(conn, t, "do-release-upgrade -c 2>&1", timeout=90)
            m = re.search(r"New release '([^']+)' available", out)
            if m:
                info.update(available=True, target_name=f"Ubuntu {m.group(1)}")
            elif "command not found" in out or code == 127:
                info["note"] = ("Install the ubuntu-release-upgrader-core package so Patchrack can check "
                                "for new Ubuntu releases.")
        elif f.os_id == "debian":
            info["family"] = "debian"
            if f.is_proxmox:
                info["unsupported"] = ("This is a Proxmox VE host. Upgrade it with Proxmox's own upgrade guide, "
                                       "which includes extra checks.")
            elif f.is_raspberry_pi_os:
                info["unsupported"] = ("This is Raspberry Pi OS. For a new release, the Raspberry Pi Foundation "
                                       "recommends a fresh install.")
            else:
                major = int((f.os_version_id or "0").split(".")[0] or 0)
                releases = await debian_releases()
                nxt = releases.get(major + 1)
                if major and nxt:
                    codename, version = nxt
                    info.update(available=True, target=codename,
                                target_name=f"Debian {version.split('.')[0]} ({codename})")
                elif not releases:
                    info["note"] = "Couldn't reach deb.debian.org to look for a new Debian release."
    except Exception as exc:  # detection is optional; never break a check
        log.warning("release detection failed for %s: %s", t.name, exc)
        info["note"] = f"Couldn't check for a new release: {exc}"
    return info


def preflight(row, release: dict) -> list[dict]:
    """The checks shown before an upgrade. Each item: {ok, text}."""
    free = release.get("free_bytes")
    helper = release.get("helper")
    pending = row["pending_count"] if row["status"] == "updates" else 0
    checks = [
        {"ok": pending == 0,
         "text": "All updates are installed" if pending == 0
         else f"{pending} update{'' if pending == 1 else 's'} waiting. Install them first."},
        {"ok": not row["reboot_required"],
         "text": "No reboot is waiting" if not row["reboot_required"] else "A reboot is waiting. Reboot first."},
        {"ok": free is not None and free >= MIN_FREE_BYTES,
         "text": (f"{free / 1e9:.1f} GB free on /" if free is not None else "Free disk space unknown")
         + ("" if free is not None and free >= MIN_FREE_BYTES else " (at least 5 GB needed)")},
        {"ok": bool(helper) and int(helper) >= HELPER_VERSION,
         "text": "Upgrade helper installed" if helper and int(helper) >= HELPER_VERSION
         else "Upgrade helper missing. Run the setup command on this server again."},
    ]
    return checks
