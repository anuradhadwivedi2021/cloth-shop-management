"""What the main counter and a second counter agree on.

The second counter does not keep a copy of the shop. Every database call it makes travels to the main PC, which runs
it on the one shop database. So both counters always see the same stock, bills and numbers.

Safety rules (each one is tested):
  * only private network addresses (192.168.x.x, 10.x.x.x, 172.16-31.x.x) and this computer (127.x.x.x) are allowed
    on both ends; an internet address is refused, so nothing can ever leave the shop network;
  * every message is encrypted and signed with a key made from a pairing code that only the owner sees (AES-GCM):
    someone on the same WiFi without the code can read nothing and change nothing;
  * every message carries a rising sequence number, so a recorded message cannot be played again;
  * only ordinary data statements are accepted (SELECT, INSERT, UPDATE, DELETE and transaction control). Nothing that
    attaches another file, changes the key, or loads code.
"""

import base64
import ipaddress
import json
import os
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

DEFAULT_PORT = 8765
_SALT = b"clothshop-lan-v1"
_AAD = b"cs1"
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I to misread
ALLOWED_FIRST_WORDS = {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "WITH", "BEGIN", "COMMIT", "END", "ROLLBACK",
                       "SAVEPOINT", "RELEASE"}
_FIRST_WORD = re.compile(r"^\s*(?:--[^\n]*\n\s*)*([A-Za-z]+)")
_BLOCKED_ANYWHERE = re.compile(r"\b(ATTACH|DETACH|PRAGMA|VACUUM|LOAD_EXTENSION)\b", re.IGNORECASE)


class LanError(Exception):
    """Something the user should be told: wrong code, main PC not reachable, address not allowed."""


def new_pairing_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def clean_code(code) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def derive_key(code) -> bytes:
    """The shared encryption key. The same code gives the same key on both PCs."""
    cleaned = clean_code(code)
    if len(cleaned) != 10:
        raise LanError("The pairing code has 10 letters and numbers, like K7M2-QX9P-4T.")
    return Scrypt(salt=_SALT, length=32, n=2 ** 14, r=8, p=1).derive(cleaned.encode())


def check_private_address(host) -> str:
    """Return the address if it is on the shop network or this computer, else raise LanError."""
    try:
        ip = ipaddress.ip_address(str(host).strip())
    except ValueError:
        raise LanError("Type the main PC's address as numbers, like 192.168.1.20.") from None
    if not (ip.is_private or ip.is_loopback) or ip.is_unspecified:
        raise LanError("Only an address on your own shop network (like 192.168.1.20) is allowed.")
    return str(ip)


def check_statement(sql) -> None:
    match = _FIRST_WORD.match(str(sql))
    if not match or match.group(1).upper() not in ALLOWED_FIRST_WORDS or _BLOCKED_ANYWHERE.search(str(sql)):
        raise LanError("That kind of statement is not allowed from a second counter.")


# ---------------------------------------------------------------- values that JSON cannot carry (bytes)

def pack_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def unpack_value(value):
    if isinstance(value, dict) and "__b" in value:
        return base64.b64decode(value["__b"])
    return value


# ---------------------------------------------------------------- encrypted messages

def seal(key: bytes, message: dict) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, json.dumps(message, separators=(",", ":")).encode(), _AAD)


def unseal(key: bytes, blob: bytes) -> dict:
    """Decrypt and check a message. Raises LanError when the code is wrong or the message was changed."""
    try:
        return json.loads(AESGCM(key).decrypt(blob[:12], blob[12:], _AAD))
    except (InvalidTag, ValueError, IndexError):
        raise LanError("The message could not be read. Check the pairing code.") from None
