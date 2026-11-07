import json

import pytest

from core import size_sets
from core.matrix_entry import save_matrix_product
from core.permissions import PermissionDenied
from db.connection import open_db


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "shop.db", "test-password")
    for name, role in (("owner", "owner"), ("till", "cashier")):
        c.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    yield c
    c.close()


def test_built_in_sets_cover_letters_waist_baby_and_free_size(conn):
    names = [s["name"] for s in size_sets.list_size_sets(conn)]
    assert any("Waist 28 to 40" == n for n in names) and "Free size" in names
    assert size_sets.sizes_text(conn, "waist 28 to 40") == "28, 30, 32, 34, 36, 38, 40"
    assert size_sets.sizes_text(conn, "Free size") == "Free Size"
    assert size_sets.sizes_text(conn, "nothing") == ""


def test_owner_adds_changes_and_deletes_a_set_and_each_step_is_audited(conn):
    size_sets.save_size_set(conn, 1, "Shoes UK", "6, 7, 8, 9, 10")
    assert size_sets.sizes_text(conn, "Shoes UK") == "6, 7, 8, 9, 10"
    size_sets.save_size_set(conn, 1, "Shoes UK 6-11", "6,7,8,9,10,11", replaces="Shoes UK")
    names = [s["name"] for s in size_sets.list_size_sets(conn)]
    assert "Shoes UK 6-11" in names and "Shoes UK" not in names
    size_sets.delete_size_set(conn, 1, "Shoes UK 6-11")
    assert "Shoes UK 6-11" not in [s["name"] for s in size_sets.list_size_sets(conn)]
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE entity = 'size_set' ORDER BY id")]
    assert actions == ["size_set_added", "size_set_changed", "size_set_deleted"]


def test_bad_input_is_refused_and_nothing_is_saved(conn):
    for name, sizes in (("", "S"), ("X", " , "), ("A" * 41, "S"), ("X", ",".join(str(i) for i in range(41))),
                        ("X", "B" * 21)):
        with pytest.raises(ValueError):
            size_sets.save_size_set(conn, 1, name, sizes)
    assert conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'size_sets'").fetchone()[0] == 0
    with pytest.raises(ValueError):
        size_sets.save_size_set(conn, 1, "free SIZE", "Free")  # same name, different case
    with pytest.raises(ValueError):
        size_sets.delete_size_set(conn, 1, "No such set")
    with pytest.raises(ValueError):
        size_sets.save_size_set(conn, 1, "New", "S", replaces="No such set")


def test_a_cashier_cannot_change_size_sets(conn):
    with pytest.raises(PermissionDenied):
        size_sets.save_size_set(conn, 2, "Mine", "S, M")
    with pytest.raises(PermissionDenied):
        size_sets.delete_size_set(conn, 2, "Free size")


def test_a_damaged_stored_value_falls_back_to_the_built_in_sets(conn):
    conn.execute("INSERT INTO settings(key, value) VALUES ('size_sets', '{not json')")
    assert size_sets.list_size_sets(conn)[0]["name"] == size_sets.DEFAULT_SIZE_SETS[0]["name"]


def test_deleting_a_set_does_not_touch_products_made_from_it(conn):
    text = size_sets.sizes_text(conn, "Waist 28 to 40")
    out = save_matrix_product(conn, 1, name="Jeans", category="Jeans", price_text="999", cost_text="500",
                              sizes_text=text, colours_text="Blue")
    assert len(out["variant_ids"]) == 7
    size_sets.delete_size_set(conn, 1, "Waist 28 to 40")
    assert conn.execute("SELECT COUNT(*) FROM variants WHERE product_id = ?", (out["product_id"],)).fetchone()[0] == 7
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key='size_sets'").fetchone()[0])
