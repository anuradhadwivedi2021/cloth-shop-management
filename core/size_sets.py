"""Size sets: ready-made lists of sizes the owner can pick instead of typing them (SRS PRD-04).

A set is a name and an ordered list of sizes, for example "Waist 28 to 40" = 28, 30, ... 40.
The owner can add, change and delete sets. Sets only fill in the sizes box when a product or its
variants are created: changing or deleting a set never touches any product, variant, stock or bill
that already exists.

Sets are stored in the shop settings (key "size_sets", JSON). Until the owner saves a change the
built-in sets below are used. Changes are audited with before and after, in one transaction.
"""

import json

from core.audit import log_event
from core.matrix_entry import split_list
from core.permissions import require_permission
from db.connection import transaction

SETTING_KEY = "size_sets"
MAX_SETS = 30
MAX_SIZES = 40
MAX_NAME = 40
MAX_SIZE_LENGTH = 20

DEFAULT_SIZE_SETS = (
    {"name": "Letter sizes (S to XXL)", "sizes": ["S", "M", "L", "XL", "XXL"]},
    {"name": "Waist 28 to 40", "sizes": ["28", "30", "32", "34", "36", "38", "40"]},
    {"name": "Baby 0 to 12 months", "sizes": ["0-3 M", "3-6 M", "6-9 M", "9-12 M"]},
    {"name": "Free size", "sizes": ["Free Size"]},
)


def _defaults() -> list:
    return [{"name": s["name"], "sizes": list(s["sizes"])} for s in DEFAULT_SIZE_SETS]


def list_size_sets(conn) -> list:
    """Every size set, in the order the owner keeps them: [{'name': ..., 'sizes': [...]}, ...]."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTING_KEY,)).fetchone()
    if row is None:
        return _defaults()
    try:
        data = json.loads(row[0])
        return [{"name": str(s["name"]), "sizes": [str(x) for x in s["sizes"]]} for s in data]
    except (ValueError, TypeError, KeyError):
        return _defaults()  # a damaged value must never stop products from being added


def sizes_text(conn, name) -> str:
    """'S, M, L, XL, XXL' for a set name, ready to put in the sizes box. '' when there is no such set."""
    for item in list_size_sets(conn):
        if item["name"].lower() == (name or "").strip().lower():
            return ", ".join(item["sizes"])
    return ""


def _clean(name, sizes_input) -> dict:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Give the size set a name.")
    if len(name) > MAX_NAME:
        raise ValueError(f"The name can have at most {MAX_NAME} characters.")
    sizes = split_list(sizes_input if isinstance(sizes_input, str) else ", ".join(sizes_input or []))
    if not sizes:
        raise ValueError("Enter at least one size.")
    if len(sizes) > MAX_SIZES:
        raise ValueError(f"A size set can have at most {MAX_SIZES} sizes.")
    if any(len(size) > MAX_SIZE_LENGTH for size in sizes):
        raise ValueError(f"A size can have at most {MAX_SIZE_LENGTH} characters.")
    return {"name": name, "sizes": sizes}


def _store(conn, user_id, before, after, action, name) -> None:
    value = json.dumps(after, ensure_ascii=False)
    with transaction(conn):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTING_KEY, value),
        )
        log_event(conn, action, user_id=user_id, entity="size_set", before=before, after=after)


def save_size_set(conn, user_id, name, sizes_input, *, replaces=None) -> list:
    """Add a size set, or change one (pass the old name in `replaces`). Returns all sets."""
    require_permission(conn, user_id, "edit_products", "change size sets")
    new = _clean(name, sizes_input)
    current = list_size_sets(conn)
    old_key = (replaces or "").strip().lower()
    if replaces is not None and not any(s["name"].lower() == old_key for s in current):
        raise ValueError("That size set no longer exists.")
    if any(s["name"].lower() == new["name"].lower() and s["name"].lower() != old_key for s in current):
        raise ValueError("A size set with this name already exists.")
    if replaces is None and len(current) >= MAX_SETS:
        raise ValueError(f"You can keep at most {MAX_SETS} size sets.")
    if replaces is None:
        updated = current + [new]
    else:
        updated = [new if s["name"].lower() == old_key else s for s in current]
    _store(conn, user_id, current, updated, "size_set_changed" if replaces else "size_set_added", new["name"])
    return updated


def delete_size_set(conn, user_id, name) -> list:
    """Remove a size set from the list. Products already made from it are not touched."""
    require_permission(conn, user_id, "edit_products", "change size sets")
    key = (name or "").strip().lower()
    current = list_size_sets(conn)
    updated = [s for s in current if s["name"].lower() != key]
    if len(updated) == len(current):
        raise ValueError("That size set no longer exists.")
    _store(conn, user_id, current, updated, "size_set_deleted", name)
    return updated
