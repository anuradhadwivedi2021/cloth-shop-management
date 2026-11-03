from types import SimpleNamespace

import pytest

from core import auth
from core.audit import verify_chain
from core.permissions import PermissionDenied
from core.supplier_admin import DuplicateSupplier, add_supplier, edit_supplier, list_suppliers
from core.supplier_ledger import set_opening_balance, set_supplier_active
from db.connection import open_db

GSTIN = "27AAPFU0939F1ZV"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    store = auth.create_user(conn, owner, "store", "store-password", "stock")
    ca = auth.create_user(conn, owner, "ca", "ca-password-1", "accountant")
    yield SimpleNamespace(conn=conn, owner=owner, till=till, store=store, ca=ca)
    conn.close()


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]


def test_owner_and_stock_staff_can_add(shop):
    a = add_supplier(shop.conn, shop.owner, "Ravi Textiles", phone="9000000001", gstin=GSTIN.lower(), state="Maharashtra")
    b = add_supplier(shop.conn, shop.store, "Sharma Mills")
    row = shop.conn.execute("SELECT * FROM suppliers WHERE id = ?", (a,)).fetchone()
    assert row["gstin"] == GSTIN and row["state"] == "Maharashtra" and row["is_active"] == 1
    assert b != a and _count(shop.conn) == 2


def test_cashier_and_accountant_cannot_add(shop):
    for user in (shop.till, shop.ca):
        with pytest.raises(PermissionDenied):
            add_supplier(shop.conn, user, "Sneaky Supplier")
    assert _count(shop.conn) == 0


def test_name_is_required(shop):
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="name is required"):
            add_supplier(shop.conn, shop.owner, bad)
    assert _count(shop.conn) == 0


@pytest.mark.parametrize("gstin", ["27AAPFU0939F1ZX", "123", "ABCDEFGHIJKLMNO"])
def test_wrong_gstin_is_refused_and_nothing_saved(shop, gstin):
    with pytest.raises(ValueError, match="GSTIN"):
        add_supplier(shop.conn, shop.owner, "Ravi Textiles", gstin=gstin)
    assert _count(shop.conn) == 0


@pytest.mark.parametrize("phone", ["abc", "12", "98765 43210 98765 43210 1"])
def test_odd_phone_is_refused(shop, phone):
    with pytest.raises(ValueError, match="Phone"):
        add_supplier(shop.conn, shop.owner, "Ravi Textiles", phone=phone)
    assert _count(shop.conn) == 0


def test_landline_style_phone_is_fine(shop):
    assert add_supplier(shop.conn, shop.owner, "Ravi Textiles", phone="+91 (20) 2612-3456")


def test_bad_email_is_refused(shop):
    with pytest.raises(ValueError, match="Email"):
        add_supplier(shop.conn, shop.owner, "Ravi Textiles", email="ravi.at.example")
    assert add_supplier(shop.conn, shop.owner, "Ravi Textiles", email="ravi@example.com")


def test_look_alike_is_warned_and_can_be_confirmed(shop):
    first = add_supplier(shop.conn, shop.owner, "Ravi Textiles", phone="9000000001", gstin=GSTIN)
    for kwargs in (
        {"name": "RAVI TEXTILES"},
        {"name": "Different Name", "phone": "9000000001"},
        {"name": "Another Name", "gstin": GSTIN},
    ):
        name = kwargs.pop("name")
        with pytest.raises(DuplicateSupplier) as err:
            add_supplier(shop.conn, shop.owner, name, **kwargs)
        assert err.value.matches[0]["id"] == first and err.value.matches[0]["reasons"]
    assert _count(shop.conn) == 1  # nothing was merged or added
    second = add_supplier(shop.conn, shop.owner, "RAVI TEXTILES", confirm_duplicate=True)
    assert second != first and _count(shop.conn) == 2


def test_adding_is_audited_and_chain_is_valid(shop):
    sid = add_supplier(shop.conn, shop.owner, "Ravi Textiles", gstin=GSTIN)
    row = shop.conn.execute("SELECT action, user_id, entity_id FROM audit_log WHERE action = 'supplier_created'").fetchone()
    assert row["user_id"] == shop.owner and row["entity_id"] == sid
    assert verify_chain(shop.conn) == (True, None)


def test_edit_changes_details_and_checks_them(shop):
    sid = add_supplier(shop.conn, shop.owner, "Ravi Textiles", phone="9000000001")
    assert edit_supplier(shop.conn, shop.store, sid, phone="9000000002", state="Gujarat") == ["phone", "state"]
    assert edit_supplier(shop.conn, shop.store, sid, phone="9000000002") == []  # nothing changed
    with pytest.raises(ValueError, match="Phone"):
        edit_supplier(shop.conn, shop.owner, sid, phone="abc")
    with pytest.raises(ValueError, match="GSTIN"):
        edit_supplier(shop.conn, shop.owner, sid, gstin="bad")
    with pytest.raises(PermissionDenied):
        edit_supplier(shop.conn, shop.till, sid, state="Goa")
    row = shop.conn.execute("SELECT phone, state FROM suppliers WHERE id = ?", (sid,)).fetchone()
    assert (row["phone"], row["state"]) == ("9000000002", "Gujarat")


def test_list_shows_balance_and_hides_inactive_by_default(shop):
    a = add_supplier(shop.conn, shop.owner, "Ravi Textiles", phone="9000000001")
    b = add_supplier(shop.conn, shop.owner, "Sharma Mills")
    set_opening_balance(shop.conn, shop.owner, a, 250050)  # we owe Rs 2,500.50
    set_supplier_active(shop.conn, shop.owner, b, False)
    rows = list_suppliers(shop.conn, shop.owner)
    assert [r["name"] for r in rows] == ["Ravi Textiles"] and rows[0]["balance_paise"] == 250050
    everyone = list_suppliers(shop.conn, shop.owner, include_inactive=True)
    assert {r["name"]: r["is_active"] for r in everyone} == {"Ravi Textiles": True, "Sharma Mills": False}


def test_list_search_and_who_may_read(shop):
    add_supplier(shop.conn, shop.owner, "Ravi Textiles", phone="9000000001")
    add_supplier(shop.conn, shop.owner, "Sharma Mills", gstin=GSTIN)
    assert [r["name"] for r in list_suppliers(shop.conn, shop.owner, query="sharma")] == ["Sharma Mills"]
    assert [r["name"] for r in list_suppliers(shop.conn, shop.owner, query="9000000001")] == ["Ravi Textiles"]
    assert len(list_suppliers(shop.conn, shop.ca)) == 2  # accountant may read
    with pytest.raises(PermissionDenied):
        list_suppliers(shop.conn, shop.till)
