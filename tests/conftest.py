"""Shared pytest config: point the app at an isolated temp data dir.

JC_DATADIR / ADMIN_* must be set before any app module is imported, so it
happens here at collection time (conftest loads before test modules).
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("JC_DATADIR", tempfile.mkdtemp(prefix="jc-tests-"))
os.environ.setdefault("ADMIN_USER", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-pass")
