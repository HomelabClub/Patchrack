# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Everything that talks to a managed server over SSH (Debian/Ubuntu, apt)."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import asyncssh

from . import crypto, db

LineCb = Callable[[str], Awaitable[None] | None]

ENV_PREFIX = ("export LC_ALL=C DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a "
              "APT_LISTCHANGES_FRONTEND=none; ")
APT_OPTS = "-o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

PROBE = r"""
. /etc/os-release 2>/dev/null
echo "OS=${PRETTY_NAME:-unknown}"
echo "ID=${ID:-} ${ID_LIKE:-}"
echo "KERNEL=$(uname -r)"
echo "UPTIME=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
echo "BOOTID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
if [ -f /var/run/reboot-required ]; then echo REBOOTFILE=1; else echo REBOOTFILE=0; fi
echo "REBOOTPKGS=$(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)"
echo "NEWESTKERNEL=$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's#.*/vmlinuz-##' | sort -V | tail -n1)"
if command -v apt-get >/dev/null 2>&1; then echo APT=1; else echo APT=0; fi
"""

INST_RE = re.compile(r"^Inst (\S+)(?: \[([^\]]+)\])? \((\S+) (.*?)(?: \[[^\]]+\])?\)")


class SSHOpError(Exception):
    """A failure with a message that is safe and useful to show in the GUI."""


class HostKeyChanged(SSHOpError):
    pass


@dataclass
class Target:
    id: int
    name: str
    host: str
    port: int
    username: str
    private_key: str
    passphrase: str | None
    become: str
    sudo_password: str | None
    upgrade_mode: str
    host_key: str | None

    @classmethod
    def from_row(cls, row) -> "Target":
        sid = row["id"]
        if row["key_source"] == "custom":
            pk = crypto.decrypt(row["private_key_enc"], f"server:{sid}:private_key")
            pp = crypto.decrypt(row["passphrase_enc"], f"server:{sid}:passphrase")
        else:
            pk = db.get_setting("app_private_key")
            pp = None
        if not pk:
            raise SSHOpError("No SSH private key is configured for this server")
        return cls(
            id=sid, name=row["name"], host=row["host"], port=row["port"], username=row["username"],
            private_key=pk, passphrase=pp, become=row["become"],
            sudo_password=crypto.decrypt(row["sudo_password_enc"], f"server:{sid}:sudo_password"),
            upgrade_mode=row["upgrade_mode"], host_key=row["host_key"],
        )


@dataclass
class Facts:
    os_name: str = ""
    kernel: str = ""
    uptime_s: int = 0
    boot_id: str = ""
    reboot_required: bool = False
    reboot_reason: str = ""
    has_apt: bool = True
    packages: list[dict] = field(default_factory=list)
    kept_back: list[str] = field(default_factory=list)

    @property
    def security_count(self) -> int:
        return sum(1 for p in self.packages if p["security"])


# ---- connection ---------------------------------------------------------------
async def connect(t: Target, timeout: int = 15) -> tuple[asyncssh.SSHClientConnection, str | None]:
    """Connect with public-key auth and trust-on-first-use host key pinning.

    Returns the connection and, on first contact, the newly seen host key (to be stored).
    """
    try:
        key = asyncssh.import_private_key(t.private_key, passphrase=t.passphrase or None)
    except (asyncssh.KeyImportError, ValueError) as exc:
        raise SSHOpError(f"The SSH private key could not be loaded: {exc}") from exc

    known = ([asyncssh.import_public_key(t.host_key)], [], []) if t.host_key else None
    try:
        conn = await asyncio.wait_for(asyncssh.connect(
            t.host, port=t.port, username=t.username, client_keys=[key], known_hosts=known,
            agent_path=None, config=None, preferred_auth="publickey", password=None,
            connect_timeout=timeout, keepalive_interval=20, keepalive_count_max=6,
        ), timeout + 5)
    except asyncssh.HostKeyNotVerifiable as exc:
        raise HostKeyChanged(
            "The server's host key changed since it was first trusted. If you reinstalled the server, "
            "clear the trusted key in its settings. Otherwise, investigate before connecting.") from exc
    except asyncssh.PermissionDenied as exc:
        raise SSHOpError(f"Login refused for {t.username}@{t.host}. Check that the public key is in "
                         f"~/.ssh/authorized_keys for that user.") from exc
    except (asyncio.TimeoutError, OSError) as exc:
        raise SSHOpError(f"Could not reach {t.host}:{t.port} ({exc.__class__.__name__}: {exc})") from exc
    except asyncssh.Error as exc:
        raise SSHOpError(f"SSH error: {exc.reason if hasattr(exc, 'reason') else exc}") from exc

    new_key = None
    if not t.host_key:
        hk = conn.get_server_host_key()
        if hk is not None:
            new_key = hk.export_public_key().decode().strip()
    return conn, new_key


def fingerprint(public_key: str | None) -> str | None:
    if not public_key:
        return None
    return asyncssh.import_public_key(public_key).get_fingerprint("sha256")


def _wrap(t: Target, cmd: str, root: bool) -> str:
    if not root or t.become == "root":
        return ENV_PREFIX + cmd
    if t.become == "sudo_password":
        return ENV_PREFIX + "sudo -S -p '' " + cmd
    return ENV_PREFIX + "sudo -n " + cmd


async def run(conn, t: Target, cmd: str, *, root: bool = False, on_line: LineCb | None = None,
              timeout: float = 600) -> tuple[int, str]:
    full = _wrap(t, cmd, root)
    proc = await conn.create_process(full, stderr=asyncssh.STDOUT, encoding="utf-8", errors="replace")
    if root and t.become == "sudo_password":
        proc.stdin.write((t.sudo_password or "") + "\n")
    proc.stdin.write_eof()
    out: list[str] = []

    async def pump():
        async for line in proc.stdout:
            line = line.rstrip("\r\n")
            # apt sometimes uses carriage returns for progress; keep the last frame
            if "\r" in line:
                line = line.split("\r")[-1]
            out.append(line)
            if on_line:
                res = on_line(line)
                if asyncio.iscoroutine(res):
                    await res
        await proc.wait()

    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise SSHOpError(f"Command timed out after {int(timeout)}s: {cmd.split()[0]}")
    code = proc.exit_status if proc.exit_status is not None else 255
    text = "\n".join(out)
    if root and t.become != "root" and code != 0 and ("a password is required" in text
                                                       or "incorrect password" in text
                                                       or "not allowed to execute" in text):
        raise SSHOpError("sudo refused the command. Run the setup script on this server, or check the "
                         "sudo settings for this server.")
    return code, text


# ---- facts --------------------------------------------------------------------
def _parse_probe(text: str, facts: Facts) -> None:
    kv = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    facts.os_name = kv.get("OS", "")
    facts.kernel = kv.get("KERNEL", "")
    try:
        facts.uptime_s = int(float(kv.get("UPTIME") or 0))
    except ValueError:
        facts.uptime_s = 0
    facts.boot_id = kv.get("BOOTID", "")
    facts.has_apt = kv.get("APT") == "1"
    reasons = []
    if kv.get("REBOOTFILE") == "1":
        pkgs = kv.get("REBOOTPKGS", "").strip()
        reasons.append("reboot-required flag set" + (f" by {pkgs}" if pkgs else ""))
    newest = kv.get("NEWESTKERNEL", "")
    if newest and facts.kernel and newest != facts.kernel:
        reasons.append(f"kernel {newest} installed, running {facts.kernel}")
    facts.reboot_required = bool(reasons)
    facts.reboot_reason = "; ".join(reasons)


def _parse_simulation(text: str, facts: Facts) -> None:
    pkgs = []
    for line in text.splitlines():
        m = INST_RE.match(line)
        if not m:
            continue
        name, old, new, origin = m.group(1), m.group(2), m.group(3), m.group(4)
        pkgs.append({
            "name": name, "from": old or "", "to": new, "origin": origin,
            "security": "-security" in origin or "security." in origin,
            "new": old is None,
        })
    pkgs.sort(key=lambda p: (not p["security"], p["name"]))
    facts.packages = pkgs
    kept, grab = [], False
    for line in text.splitlines():
        if line.startswith("The following packages have been kept back"):
            grab = True
            continue
        if grab:
            if line.startswith("  "):
                kept.extend(line.split())
            else:
                grab = False
    facts.kept_back = kept


async def gather_facts(conn, t: Target, on_line: LineCb | None = None, refresh: bool = True) -> Facts:
    facts = Facts()
    if refresh:
        if on_line:
            await _maybe(on_line, "$ apt-get update")
        code, out = await run(conn, t, f"apt-get {APT_OPTS} -q update", root=True, on_line=on_line,
                              timeout=300)
        if code != 0:
            raise SSHOpError("apt-get update failed (exit %d). See the log for details." % code)
    code, out = await run(conn, t, PROBE, timeout=30)
    _parse_probe(out, facts)
    if not facts.has_apt:
        raise SSHOpError("apt-get was not found. Patchrack manages Debian and Ubuntu servers only.")
    mode = "dist-upgrade" if t.upgrade_mode == "full" else "upgrade"
    code, out = await run(conn, t, f"apt-get -s -o Debug::NoLocking=1 {mode}", timeout=120)
    if code != 0:
        last = out.strip().splitlines()[-1] if out.strip() else f"exit {code}"
        raise SSHOpError(f"apt-get simulation failed: {last}")
    _parse_simulation(out, facts)
    return facts


async def _maybe(cb: LineCb, line: str):
    res = cb(line)
    if asyncio.iscoroutine(res):
        await res


async def test_sudo(conn, t: Target) -> tuple[bool, str]:
    if t.become == "root":
        code, out = await run(conn, t, "id -u", timeout=15)
        ok = out.strip().endswith("0")
        return ok, "Connected as root" if ok else "This user is not root. Choose sudo instead."
    flag = "-S -p ''" if t.become == "sudo_password" else "-n"
    proc = await conn.create_process(ENV_PREFIX + f"sudo {flag} -l apt-get", stderr=asyncssh.STDOUT,
                                     encoding="utf-8", errors="replace")
    if t.become == "sudo_password":
        proc.stdin.write((t.sudo_password or "") + "\n")
    proc.stdin.write_eof()
    await asyncio.wait_for(proc.wait(), 15)
    if proc.exit_status == 0:
        return True, "sudo can run apt-get"
    if t.become == "sudo_password":
        return False, "sudo rejected the password, or this user may not run apt-get"
    return False, "sudo cannot run apt-get without a password. Run the setup script on this server."


async def wait_for_reboot(t: Target, old_boot_id: str, timeout_s: int, on_line: LineCb) -> Facts:
    """Poll until the server is back with a new boot id, then return fresh facts."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    await asyncio.sleep(10)
    attempt = 0
    while loop.time() < deadline:
        attempt += 1
        try:
            conn, _ = await connect(t, timeout=8)
        except HostKeyChanged:
            raise
        except SSHOpError:
            if attempt % 3 == 1:
                await _maybe(on_line, "… waiting for the server to come back")
            await asyncio.sleep(5)
            continue
        try:
            code, out = await run(conn, t, "cat /proc/sys/kernel/random/boot_id", timeout=10)
            if out.strip() and out.strip() != old_boot_id:
                await _maybe(on_line, "Server is back online.")
                return await gather_facts(conn, t, on_line=None, refresh=False)
        finally:
            conn.close()
        await asyncio.sleep(5)
    raise SSHOpError(f"The server did not come back within {timeout_s // 60} minutes after rebooting")

