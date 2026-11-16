# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""The automatic update script: turning it on and off, and what a run does.

Everything runs on temporary folders, with apt-get and the reboot replaced by stand-ins.
"""
import os
import pathlib
import subprocess
import textwrap

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "app" / "auto_update.sh"
pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="the script checks for root")


@pytest.fixture
def box(tmp_path):
    (tmp_path / "bin").mkdir()
    env = {"PATCHRACK_CONF_DIR": str(tmp_path / "etc"), "PATCHRACK_CRON_FILE": str(tmp_path / "cron"),
           "PATCHRACK_STATE_DIR": str(tmp_path / "state"), "PATCHRACK_LOG": str(tmp_path / "auto.log"),
           "PATCHRACK_REBOOT_FILE": str(tmp_path / "reboot-required"),
           "PATCHRACK_LOCK": str(tmp_path / "lock")}
    return tmp_path, env


def bash(box, script):
    d, env = box
    # stand-ins: cron is "installed", apt-get comes from bin/, rebooting just leaves a mark
    full = (f'source "{SCRIPT}"\nensure_cron() {{ :; }}\ndo_reboot() {{ touch "{d}/rebooted"; }}\n'
            f'PATH="{d}/bin:$PATH"\n' + textwrap.dedent(script))
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True, env={**os.environ, **env})


def state(d):
    return dict(l.split("=", 1) for l in (d / "state" / "auto-update.state").read_text().splitlines())


def fake(d, name, body):
    p = d / "bin" / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)


def test_enable_writes_cron_job_and_settings(box):
    d, _ = box
    r = bash(box, "enable 30 3 1,4 full 1 0")
    assert r.returncode == 0, r.stdout + r.stderr
    cron = (d / "cron").read_text()
    assert "30 3 * * 1,4 root /usr/local/sbin/patchrack-auto-update run" in cron
    conf = (d / "etc" / "auto-update.conf").read_text()
    assert "mode=full" in conf and "containers=0" in conf
    assert "on Mon, Thu at 03:30" in r.stdout
    r = bash(box, "status")
    assert "enabled=1" in r.stdout and "conf_hour=3" in r.stdout and "state=none" in r.stdout


@pytest.mark.parametrize("args", ["60 3 * full 1 0", "0 24 * full 1 0", "0 3 7 full 1 0", "0 3 1;rm full 1 0",
                                  "0 3 * risky 1 0", "0 3 * full 2 0", "0 3 '*; id' full 1 0"])
def test_enable_refuses_bad_input(box, args):
    d, _ = box
    r = bash(box, f"enable {args}")
    assert r.returncode == 2
    assert not (d / "cron").exists()


def test_disable_removes_cron_job_and_settings(box):
    d, _ = box
    bash(box, "enable 0 4 '*' safe 0 0")
    r = bash(box, "disable")
    assert r.returncode == 0
    assert not (d / "cron").exists() and not (d / "etc" / "auto-update.conf").exists()
    assert "enabled=0" in bash(box, "status").stdout


def test_run_does_nothing_when_turned_off(box):
    d, _ = box
    fake(d, "apt-get", 'echo called >> "$(dirname "$0")/../apt-called"\n')
    assert bash(box, "run").returncode == 0
    assert not (d / "apt-called").exists() and not (d / "state").exists()


APT_OK = textwrap.dedent("""\
    case "$*" in
      *" -s "*|*"-s -o"*) echo "Inst libc6 [2.39-0] (2.39-1 Ubuntu:24.04/noble-updates [amd64])"
                          echo "Inst curl [8.5] (8.6 Ubuntu:24.04/noble-security [amd64])" ;;
      *) echo "apt-get $*" ;;
    esac
    """)


def test_successful_run_records_packages_and_reboots_when_needed(box):
    d, _ = box
    fake(d, "apt-get", APT_OK)
    fake(d, "dpkg", "exit 0\n")
    bash(box, "enable 0 3 '*' full 1 0")
    (d / "reboot-required").write_text("*** System restart required ***\n")
    r = bash(box, "run")
    assert r.returncode == 0, r.stderr
    st = state(d)
    assert st["state"] == "success" and st["packages"] == "2" and st["reboot"] == "1"
    assert (d / "rebooted").exists()
    log = (d / "auto.log").read_text()
    assert "apt-get -o DPkg::Lock::Timeout=900" in log and "dist-upgrade" in log and "autoremove" in log
    assert "rebooting now" in log


def test_failed_update_is_recorded_and_never_reboots(box):
    d, _ = box
    fake(d, "apt-get", 'case "$*" in *update*) exit 0;; -s*) echo "Inst x [1] (2 a [amd64])";; '
                       '*) echo "E: Sub-process /usr/bin/dpkg returned an error code (1)"; exit 100;; esac\n')
    fake(d, "dpkg", "exit 0\n")
    bash(box, "enable 0 3 '*' safe 0 1")
    (d / "reboot-required").write_text("x\n")
    bash(box, "run")
    st = state(d)
    assert st["state"] == "failed" and st["reboot"] == "0"
    assert not (d / "rebooted").exists()
    assert "Containers were skipped because the system update failed" in (d / "auto.log").read_text()


def test_previous_log_is_kept(box):
    d, _ = box
    fake(d, "apt-get", APT_OK)
    fake(d, "dpkg", "exit 0\n")
    bash(box, "enable 0 3 '*' full 0 0")
    bash(box, "run")
    bash(box, "run")
    assert (d / "auto.log.1").exists() and (d / "auto.log").exists()


def test_run_is_skipped_during_a_release_upgrade(box):
    d, _ = box
    fake(d, "apt-get", 'echo called >> "$(dirname "$0")/../apt-called"\n')
    bash(box, "enable 0 3 '*' full 0 0")
    r = bash(box, 'release_upgrade_running() { return 0; }\nrun')
    assert r.returncode == 0
    assert state(d)["state"] == "skipped" and not (d / "apt-called").exists()


def test_time_zone_is_the_one_cron_uses(box):
    # a login session may set TZ; cron uses the system time zone, and so must the script
    d, env = box
    full = f'source "{SCRIPT}"\ndate +%Z'
    r = subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                       env={**os.environ, **env, "TZ": "Pacific/Kiritimati"})
    plain = subprocess.run(["bash", "-c", "unset TZ; date +%Z"], capture_output=True, text=True)
    assert r.stdout.strip() == plain.stdout.strip()
