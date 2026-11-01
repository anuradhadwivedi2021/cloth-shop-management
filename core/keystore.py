"""Key file: the database key, protected by passwords and a recovery key.

The database is encrypted with a random 256-bit key. That key is never stored
in the clear. The key file (shop.keys) holds one copy of it per "slot", each
encrypted with a key derived (scrypt) from that slot's password:

    one slot per user (slot name = username)   +   one 'recovery' slot

Signing in unlocks the user's slot and opens the database with the key.
If every password is lost, the printed recovery key still unlocks the data.
If the recovery key AND all passwords are lost, nothing can recover the data.

Wrong passwords are throttled (same doubling lock-out as the database logins).
"""

import base64
import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KDF_N = 2 ** 15
RECOVERY_SLOT = "recovery"
MAX_FAILED = 5
BASE_LOCK_SECONDS = 30
MAX_LOCK_SECONDS = 900
_FMT = "%Y-%m-%d %H:%M:%S"


class KeystoreError(Exception):
    pass


class KeystoreLocked(KeystoreError):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"Too many wrong attempts. Try again in {seconds} seconds.")


def _slot_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not name:
        raise KeystoreError("Slot name is required.")
    return name


def _normalise_secret(slot: str, secret: str) -> bytes:
    if slot == RECOVERY_SLOT:
        secret = "".join(ch for ch in (secret or "").upper() if ch.isalnum())
    return (secret or "").encode("utf-8")


def _kek(secret: bytes, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=KDF_N, r=8, p=1).derive(secret)


def _wrap(slot: str, secret: bytes, db_key: bytes) -> dict:
    salt, nonce = os.urandom(16), os.urandom(12)
    wrapped = AESGCM(_kek(secret, salt)).encrypt(nonce, db_key, slot.encode())
    return {"salt": salt.hex(), "nonce": nonce.hex(), "wrapped": wrapped.hex(), "failures": 0, "locked_until": None}


def _read(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("slots"), dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        raise KeystoreError("The key file is missing or damaged.") from None


def _write(path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # all-or-nothing, a crash never leaves half a file


def new_recovery_key() -> str:
    """24 letters/digits in groups of 4, e.g. 'ABCD-EFGH-...' (about 120 bits)."""
    raw = base64.b32encode(secrets.token_bytes(15)).decode().rstrip("=")  # 24 chars
    return "-".join(raw[i:i + 4] for i in range(0, 24, 4))


def create_keystore(path, first_slot: str, password: str):
    """New shop: returns (db_key_hex, recovery_key). Refuses to overwrite an existing file."""
    if Path(path).exists():
        raise KeystoreError("A key file already exists here.")
    first = _slot_name(first_slot)
    db_key = os.urandom(32)
    recovery = new_recovery_key()
    data = {
        "version": 1,
        "slots": {
            first: _wrap(first, _normalise_secret(first, password), db_key),
            RECOVERY_SLOT: _wrap(RECOVERY_SLOT, _normalise_secret(RECOVERY_SLOT, recovery), db_key),
        },
    }
    _write(path, data)
    return db_key.hex(), recovery


def unlock_keystore(path, slot: str, secret: str, *, now=None) -> str:
    """Return the database key (hex) for a correct password / recovery key, else raise."""
    now = now or datetime.now()
    slot = _slot_name(slot)
    data = _read(path)
    entry = data["slots"].get(slot)
    if entry is None:
        raise KeystoreError("Wrong username or password.")
    if entry.get("locked_until"):
        left = (datetime.strptime(entry["locked_until"], _FMT) - now).total_seconds()
        if left > 0:
            raise KeystoreLocked(int(left) + 1)
    try:
        key = AESGCM(_kek(_normalise_secret(slot, secret), bytes.fromhex(entry["salt"]))).decrypt(
            bytes.fromhex(entry["nonce"]), bytes.fromhex(entry["wrapped"]), slot.encode()
        )
    except InvalidTag:
        entry["failures"] = entry.get("failures", 0) + 1
        secs = 0 if entry["failures"] < MAX_FAILED else min(
            BASE_LOCK_SECONDS * 2 ** (entry["failures"] - MAX_FAILED), MAX_LOCK_SECONDS
        )
        entry["locked_until"] = (now + timedelta(seconds=secs)).strftime(_FMT) if secs else None
        _write(path, data)
        raise KeystoreError(
            "Wrong recovery key." if slot == RECOVERY_SLOT else "Wrong username or password."
        ) from None
    if entry.get("failures") or entry.get("locked_until"):
        entry["failures"], entry["locked_until"] = 0, None
        _write(path, data)
    return key.hex()


def set_slot(path, db_key_hex: str, slot: str, password: str) -> None:
    """Add a slot, or replace its password (needs the database key, i.e. a signed-in owner)."""
    slot = _slot_name(slot)
    if slot == RECOVERY_SLOT:
        raise KeystoreError("Use rotate_recovery_key for the recovery slot.")
    data = _read(path)
    data["slots"][slot] = _wrap(slot, _normalise_secret(slot, password), bytes.fromhex(db_key_hex))
    _write(path, data)


def remove_slot(path, slot: str) -> None:
    slot = _slot_name(slot)
    if slot == RECOVERY_SLOT:
        raise KeystoreError("The recovery slot cannot be removed.")
    data = _read(path)
    data["slots"].pop(slot, None)
    _write(path, data)


def rotate_recovery_key(path, db_key_hex: str) -> str:
    """Make a new recovery key; the old one stops working. Returns the new key."""
    data = _read(path)
    recovery = new_recovery_key()
    data["slots"][RECOVERY_SLOT] = _wrap(RECOVERY_SLOT, _normalise_secret(RECOVERY_SLOT, recovery), bytes.fromhex(db_key_hex))
    _write(path, data)
    return recovery


def slot_names(path) -> list:
    return sorted(s for s in _read(path)["slots"] if s != RECOVERY_SLOT)
