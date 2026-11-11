"""Make password hashing fast in tests (real settings are used in the app)."""

import pytest
from argon2 import PasswordHasher

from core import auth, keystore


@pytest.fixture(autouse=True)
def fast_crypto(monkeypatch):
    monkeypatch.setattr(auth, "_ph", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setattr(keystore, "KDF_N", 2 ** 10)


@pytest.fixture(autouse=True)
def no_real_printer(monkeypatch):
    """A test must never reach a real printer: on a PC with "Microsoft Print To PDF" a test that confirms a purchase
    and says yes to "print labels" would open a Save dialog and hang or crash. Say there is no default printer."""
    try:
        from PySide6.QtPrintSupport import QPrinterInfo
    except ImportError:
        return
    monkeypatch.setattr(QPrinterInfo, "defaultPrinter", staticmethod(lambda: QPrinterInfo()))