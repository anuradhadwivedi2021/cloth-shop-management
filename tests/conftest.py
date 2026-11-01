"""Make password hashing fast in tests (real settings are used in the app)."""

import pytest
from argon2 import PasswordHasher

from core import auth, keystore


@pytest.fixture(autouse=True)
def fast_crypto(monkeypatch):
    monkeypatch.setattr(auth, "_ph", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setattr(keystore, "KDF_N", 2 ** 10)
