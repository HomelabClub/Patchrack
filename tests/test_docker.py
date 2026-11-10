# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
import pytest

from app.dockerops import _create_body, familiar, human_bytes, split_ref

CID = "a" * 64
ANON = "b" * 64


@pytest.mark.parametrize("ref, expected", [
    ("nginx", ("nginx", None, None)),
    ("nginx:1.27", ("nginx", "1.27", None)),
    ("lscr.io/linuxserver/sonarr:latest", ("lscr.io/linuxserver/sonarr", "latest", None)),
    ("registry.local:5000/team/app", ("registry.local:5000/team/app", None, None)),
    ("registry.local:5000/team/app:2", ("registry.local:5000/team/app", "2", None)),
    ("ghcr.io/x/y@sha256:" + "c" * 64, ("ghcr.io/x/y", None, "sha256:" + "c" * 64)),
])
def test_split_ref(ref, expected):
    assert split_ref(ref) == expected


@pytest.mark.parametrize("repo, expected", [
    ("docker.io/library/nginx", "nginx"),
    ("library/nginx", "nginx"),
    ("docker.io/jellyfin/jellyfin", "jellyfin/jellyfin"),
    ("lscr.io/linuxserver/radarr", "lscr.io/linuxserver/radarr"),
])
def test_familiar(repo, expected):
    assert familiar(repo) == expected


def test_human_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(129_400_000) == "129.4 MB"
    assert human_bytes(32_000_000_000) == "32.0 GB"


def _info(**over):
    info = {
        "Id": CID,
        "Config": {"Image": "old", "Hostname": CID[:12], "Env": ["TZ=Europe/Amsterdam"],
                   "Labels": {"com.docker.compose.project": "media"}, "MacAddress": "02:42:ac:11:00:02"},
        "HostConfig": {"NetworkMode": "media_back", "Binds": ["media_appdata:/config:rw"],
                       "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "18081"}]},
                       "RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": [{"Type": "volume", "Name": "media_appdata", "Destination": "/config"}],
        "NetworkSettings": {"Networks": {
            "media_back": {"Aliases": ["webapp", CID[:12]], "IPAMConfig": None, "NetworkID": "n1"},
            "media_front": {"Aliases": ["webapp"], "NetworkID": "n2"}}},
    }
    info.update(over)
    return info


def test_recreate_keeps_settings_and_swaps_image():
    body, extra = _create_body(_info(), "nginx:latest")
    assert body["Image"] == "nginx:latest"
    assert body["Env"] == ["TZ=Europe/Amsterdam"]
    assert body["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"] == "18081"
    assert body["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert body["Labels"]["com.docker.compose.project"] == "media"
    assert "Hostname" not in body          # was the old container id
    assert "MacAddress" not in body


def test_recreate_attaches_first_network_then_the_rest():
    body, extra = _create_body(_info(), "img")
    first = body["NetworkingConfig"]["EndpointsConfig"]
    assert list(first) == ["media_back"]
    assert first["media_back"]["Aliases"] == ["webapp"]   # old short id alias dropped
    assert "NetworkID" not in first["media_back"]
    assert list(extra) == ["media_front"]


def test_compose_anonymous_volume_keeps_its_data():
    info = _info()
    info["HostConfig"]["Mounts"] = [{"Type": "volume", "Target": "/cachevol"}]
    info["Mounts"].append({"Type": "volume", "Name": ANON, "Destination": "/cachevol"})
    body, _ = _create_body(info, "img")
    assert body["HostConfig"]["Mounts"] == [{"Type": "volume", "Target": "/cachevol", "Source": ANON}]
    assert not any("/cachevol" in b for b in body["HostConfig"]["Binds"])


def test_docker_run_anonymous_volume_keeps_its_data():
    info = _info()
    info["Mounts"].append({"Type": "volume", "Name": ANON, "Destination": "/anon"})
    body, _ = _create_body(info, "img")
    assert f"{ANON}:/anon" in body["HostConfig"]["Binds"]
    assert "media_appdata:/config:rw" in body["HostConfig"]["Binds"]


def test_shared_network_container_drops_hostname_and_networks():
    info = _info()
    info["Config"]["Hostname"] = "vpn-host"
    info["HostConfig"]["NetworkMode"] = "container:" + "d" * 64
    body, extra = _create_body(info, "img", network_mode="container:" + "e" * 64)
    assert body["HostConfig"]["NetworkMode"] == "container:" + "e" * 64
    assert "Hostname" not in body
    assert "NetworkingConfig" not in body and extra == {}
