# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
from app.sshops import Facts, _parse_probe, _parse_simulation

SIMULATION = """\
NOTE: This is only a simulation!
Reading package lists...
The following packages have been kept back:
  ubuntu-advantage-tools
The following packages will be upgraded:
  curl openssl
Inst openssl [3.0.13-0ubuntu3.5] (3.0.13-0ubuntu3.6 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst curl [8.5.0-2ubuntu10.6] (8.5.0-2ubuntu10.7 Ubuntu:24.04/noble-updates [amd64])
Inst linux-image-6.8.0-90-generic (6.8.0-90.91 Ubuntu:24.04/noble-security [amd64])
Inst libssl3t64 [3.0.13-0ubuntu3.5] (3.0.13-0ubuntu3.6 Debian-Security:12/stable-security [amd64])
Conf openssl (3.0.13-0ubuntu3.6 Ubuntu:24.04/noble-updates [amd64])
"""


def test_simulation_lists_packages_with_security_first():
    f = Facts()
    _parse_simulation(SIMULATION, f)
    names = [p["name"] for p in f.packages]
    assert names == ["libssl3t64", "linux-image-6.8.0-90-generic", "openssl", "curl"]
    assert f.security_count == 3
    by = {p["name"]: p for p in f.packages}
    assert by["openssl"]["from"] == "3.0.13-0ubuntu3.5" and by["openssl"]["to"] == "3.0.13-0ubuntu3.6"
    assert by["linux-image-6.8.0-90-generic"]["new"] is True
    assert by["curl"]["security"] is False
    assert f.kept_back == ["ubuntu-advantage-tools"]


def test_simulation_with_nothing_to_do():
    f = Facts()
    _parse_simulation("Reading package lists...\n0 upgraded, 0 newly installed\n", f)
    assert f.packages == [] and f.kept_back == []


def test_probe_detects_reboot_flag_and_new_kernel():
    f = Facts()
    _parse_probe("OS=Ubuntu 24.04.3 LTS\nKERNEL=6.8.0-85-generic\nUPTIME=12345.67\nBOOTID=abc\n"
                 "REBOOTFILE=1\nREBOOTPKGS=linux-image-6.8.0-90-generic \n"
                 "NEWESTKERNEL=6.8.0-90-generic\nAPT=1\n", f)
    assert f.os_name == "Ubuntu 24.04.3 LTS"
    assert f.uptime_s == 12345 and f.boot_id == "abc" and f.has_apt
    assert f.reboot_required
    assert "linux-image-6.8.0-90-generic" in f.reboot_reason
    assert "running 6.8.0-85-generic" in f.reboot_reason


def test_probe_without_reboot_needed():
    f = Facts()
    _parse_probe("OS=Debian GNU/Linux 12 (bookworm)\nKERNEL=6.1.0-40-amd64\nREBOOTFILE=0\n"
                 "NEWESTKERNEL=6.1.0-40-amd64\nAPT=1\n", f)
    assert not f.reboot_required and f.reboot_reason == ""


def test_probe_flags_missing_apt():
    f = Facts()
    _parse_probe("OS=Fedora\nAPT=0\n", f)
    assert not f.has_apt
