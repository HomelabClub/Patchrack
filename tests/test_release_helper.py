# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""The release upgrade helper: switching Debian's package sources, and running an upgrade.

The helper's functions are tested on temporary folders; nothing on this machine is changed.
"""
import os
import pathlib
import subprocess
import textwrap
import time

import pytest

HELPER = pathlib.Path(__file__).resolve().parents[1] / "app" / "release_helper.sh"


def bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full = f'source "{HELPER}"\n' + textwrap.dedent(script)
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


@pytest.fixture
def apt(tmp_path):
    (tmp_path / "sources.list.d").mkdir()
    return tmp_path


def switch(apt, old="bookworm", new="trixie"):
    r = bash(f'switch_debian_sources {old} {new}', {"PATCHRACK_APT_DIR": str(apt)})
    assert r.returncode == 0, r.stderr
    return int(r.stdout.strip()), r.stderr


def test_one_line_format(apt):
    (apt / "sources.list").write_text(textwrap.dedent("""\
        # bookworm is the current release
        deb http://deb.debian.org/debian bookworm main contrib non-free-firmware
        deb-src http://deb.debian.org/debian bookworm main
        deb http://deb.debian.org/debian bookworm-updates main
        deb http://security.debian.org/debian-security bookworm-security main
        deb [arch=amd64] http://ftp.nl.debian.org/debian/ bookworm-backports main
        #deb http://deb.debian.org/debian bookworm-proposed-updates main
        """))
    (apt / "sources.list.d" / "docker.list").write_text(
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable\n")
    n, err = switch(apt)
    assert n == 5
    text = (apt / "sources.list").read_text()
    assert "deb http://deb.debian.org/debian trixie main contrib non-free-firmware" in text
    assert "deb-src http://deb.debian.org/debian trixie main" in text
    assert "trixie-updates main" in text and "trixie-security main" in text and "trixie-backports main" in text
    assert "# bookworm is the current release" in text                  # comments untouched
    assert "#deb http://deb.debian.org/debian bookworm-proposed-updates" in text
    assert "download.docker.com/linux/debian bookworm stable" in (apt / "sources.list.d" / "docker.list").read_text()
    assert "docker.list" in err                                          # third-party source is reported


def test_deb822_format(apt):
    (apt / "sources.list.d" / "debian.sources").write_text(textwrap.dedent("""\
        Types: deb deb-src
        URIs: http://deb.debian.org/debian
        Suites: bookworm bookworm-updates
        Components: main non-free-firmware
        Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

        Types: deb
        URIs: http://security.debian.org/debian-security
        Suites: bookworm-security
        Components: main
        """))
    (apt / "sources.list.d" / "grafana.sources").write_text(textwrap.dedent("""\
        Types: deb
        URIs: https://apt.grafana.com
        Suites: bookworm
        Components: main
        """))
    n, err = switch(apt)
    assert n == 2
    text = (apt / "sources.list.d" / "debian.sources").read_text()
    assert "Suites: trixie trixie-updates" in text and "Suites: trixie-security" in text
    assert "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg" in text
    assert text.count("\n\n") == 1                                        # stanza layout kept
    assert "Suites: bookworm" in (apt / "sources.list.d" / "grafana.sources").read_text()
    assert "apt.grafana.com" in err


def test_nothing_official_means_nothing_switched(apt):
    (apt / "sources.list").write_text("deb http://mirror.example.net/debian bookworm main\n")
    n, _ = switch(apt)
    assert n == 0
    assert "bookworm main" in (apt / "sources.list").read_text()


def test_codename_inside_other_words_is_left_alone(apt):
    (apt / "sources.list").write_text("deb http://deb.debian.org/debian bookworm main bookwormish-extra\n")
    n, _ = switch(apt)
    assert n == 1
    assert (apt / "sources.list").read_text() == "deb http://deb.debian.org/debian trixie main bookwormish-extra\n"


# ---- running an upgrade, with the real upgrader replaced by a stand-in ----------------------
@pytest.fixture
def box(tmp_path):
    """A fake system: os-release, state folder, log, and a stand-in do-release-upgrade."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.5 LTS"\n')
    env = {"PATCHRACK_STATE_DIR": str(tmp_path / "state"), "PATCHRACK_LOG": str(tmp_path / "upgrade.log"),
           "PATCHRACK_OS_RELEASE": str(tmp_path / "os-release"), "PATCHRACK_APT_DIR": str(tmp_path / "apt")}
    (tmp_path / "apt" / "sources.list.d").mkdir(parents=True)
    return tmp_path, env


def fake_upgrader(box_dir, body):
    p = box_dir / "bin" / "do-release-upgrade"
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)


def run_upgrade(box_dir, env):
    # call do_run directly (what the background service runs), with the stand-in first on PATH
    r = bash(f'PATH="{box_dir}/bin:$PATH"; run_ubuntu() {{ do-release-upgrade -f X </dev/null; }}; '
             'do_run ubuntu -', env)
    state = dict(l.split("=", 1) for l in (box_dir / "state" / "release-upgrade.state").read_text().splitlines())
    return r, state, (box_dir / "upgrade.log").read_text()


def test_successful_upgrade_is_recorded(box):
    d, env = box
    fake_upgrader(d, f'echo "Upgrading packages"; sed -i \'s/24.04.5 LTS/26.04.1 LTS/; s/"24.04"/"26.04"/\' "{d}/os-release"\n')
    r, state, logtext = run_upgrade(d, env)
    assert state["state"] == "success" and state["exit"] == "0"
    assert state["from"] == "Ubuntu 24.04.5 LTS" and state["to"] == "Ubuntu 26.04.1 LTS"
    assert "Upgrading packages" in logtext


def test_upgrade_that_changes_nothing_counts_as_failed(box):
    d, env = box
    fake_upgrader(d, 'echo "nothing happened"; exit 0\n')
    _, state, logtext = run_upgrade(d, env)
    assert state["state"] == "failed"
    assert "still reports version" in logtext


def test_failed_upgrade_is_recorded(box):
    d, env = box
    fake_upgrader(d, 'echo "E: broken packages"; exit 1\n')
    _, state, _ = run_upgrade(d, env)
    assert state["state"] == "failed" and state["exit"] == "1"


def test_status_and_version_work_without_root(box):
    d, env = box
    r = subprocess.run(["bash", str(HELPER), "version"], capture_output=True, text=True)
    assert r.stdout.strip() == "patchrack-release-upgrade 1"
    r = subprocess.run(["bash", str(HELPER), "status"], capture_output=True, text=True, env={**os.environ, **env})
    assert r.stdout.strip() == "state=none"


def test_stale_running_state_is_reported(box):
    d, env = box
    (d / "state").mkdir()
    (d / "state" / "release-upgrade.state").write_text("state=running\npid=999999999\n")
    r = subprocess.run(["bash", str(HELPER), "status"], capture_output=True, text=True, env={**os.environ, **env})
    assert "stale=1" in r.stdout


def test_start_refuses_bad_input_and_non_root():
    r = subprocess.run(["bash", str(HELPER), "start", "debian", "trixie; rm -rf /"], capture_output=True, text=True)
    assert r.returncode != 0
    if os.geteuid() == 0:
        assert "Invalid release name" in r.stdout
    else:
        assert "Run this as root" in r.stdout
