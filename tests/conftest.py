# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Test setup: a throwaway data folder and master secret, set before the app is imported."""
import os
import pathlib
import sys
import tempfile

os.environ["PATCHRACK_DATA"] = tempfile.mkdtemp(prefix="patchrack-test-")
os.environ["PATCHRACK_SECRET"] = "test-only-master-secret-" + "x" * 40
os.environ.pop("PATCHRACK_SECRET_FILE", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
