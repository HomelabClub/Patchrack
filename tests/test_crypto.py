# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
import pytest

from app import crypto


def test_round_trip():
    token = crypto.encrypt("-----BEGIN OPENSSH PRIVATE KEY-----", "server:1:private_key")
    assert token.startswith("v1:")
    assert "OPENSSH" not in token
    assert crypto.decrypt(token, "server:1:private_key") == "-----BEGIN OPENSSH PRIVATE KEY-----"


def test_same_value_encrypts_differently_each_time():
    assert crypto.encrypt("secret", "ctx") != crypto.encrypt("secret", "ctx")


def test_ciphertext_is_bound_to_its_record():
    token = crypto.encrypt("sudo-password", "server:1:sudo_password")
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(token, "server:2:sudo_password")


def test_tampering_is_detected():
    token = crypto.encrypt("value", "ctx")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(tampered, "ctx")


def test_empty_values_are_not_stored():
    assert crypto.encrypt(None, "ctx") is None
    assert crypto.encrypt("", "ctx") is None
    assert crypto.decrypt(None, "ctx") is None


def test_password_hashing():
    stored = crypto.hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert crypto.verify_password("correct horse battery", stored)
    assert not crypto.verify_password("wrong", stored)
    assert not crypto.verify_password("anything", "not-a-hash")
