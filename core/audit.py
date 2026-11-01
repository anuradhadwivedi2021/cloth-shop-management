"""Tamper-evident audit log.

Every entry stores the hash of the previous entry, so editing or removing an
old row breaks the chain and `verify_chain` reports where. The database also
blocks UPDATE/DELETE on this table; the hash chain catches anyone who edits
the database file directly.
"""

import hashlib
import json
from datetime import datetime


def _hash(prev_hash, user_id, action, entity, entity_id, before_json, after_json, created_at) -> str:
    payload = json.dumps(
        [prev_hash or "", user_id, action, entity, entity_id, before_json, after_json, created_at],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _to_json(value):
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def log_event(conn, action, *, user_id=None, entity=None, entity_id=None, before=None, after=None) -> int:
    """Append one audit entry. Must be called inside `with transaction(conn):`.

    Saved in the same transaction as the action it describes, so the log can
    never show something that did not happen (or miss something that did).
    """
    if not conn.in_transaction:
        raise RuntimeError("log_event must be called inside a transaction.")
    action = (action or "").strip()
    if not action:
        raise ValueError("An audit entry needs an action.")

    before_json, after_json = _to_json(before), _to_json(after)
    last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = last[0] if last else None
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry_hash = _hash(prev_hash, user_id, action, entity, entity_id, before_json, after_json, created_at)

    cur = conn.execute(
        "INSERT INTO audit_log(user_id, action, entity, entity_id, before_json, after_json, "
        "created_at, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, action, entity, entity_id, before_json, after_json, created_at, prev_hash, entry_hash),
    )
    return cur.lastrowid


def verify_chain(conn):
    """Check the whole log. Returns (True, None) or (False, id_of_first_bad_entry)."""
    prev_hash = None
    for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        if row["prev_hash"] != prev_hash:
            return False, row["id"]
        expected = _hash(
            row["prev_hash"], row["user_id"], row["action"], row["entity"], row["entity_id"],
            row["before_json"], row["after_json"], row["created_at"],
        )
        if row["hash"] != expected:
            return False, row["id"]
        prev_hash = row["hash"]
    return True, None
