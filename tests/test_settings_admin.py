"""GST rate rules, document number prefixes and the default rate table.

The rules that matter: a rate is never edited or deleted (an old bill can always be explained), a prefix never
renames or reuses a number, nothing changes without the permission, and every change is in the audit log."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import series_admin as sa
from core import shop
from core import shop_settings as ss
from core import tax_admin as ta
from core.audit import verify_chain
from core.numbering import financial_year, next_number
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.sales import create_sale
from core.stock import add_movement
from core.tax import DEFAULT_TAX_RULES, ensure_default_tax_rules, load_tax_rules
from db.connection import open_db, transaction

GSTIN = "27AAPFU0939F1ZV"  # correct check character, state 27
TODAY = date.today()


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    for name, role in (("owner", "owner"), ("till", "cashier")):
        conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    ensure_default_tax_rules(conn)
    yield SimpleNamespace(conn=conn, owner=1, cashier=2)
    conn.close()


def _bill(s, qty=1):
    """One saved GST bill today for HSN 6205 (a shirt at Rs 1,050)."""
    if not ss.get_shop_profile(s.conn)["is_set_up"]:
        ss.update_shop_profile(s.conn, s.owner, name="Anu Cloth House", gstin=GSTIN)
    if getattr(s, "variant", None) is None:  # one shirt for the whole test (a second one would be a duplicate)
        pid = create_product(s.conn, "Cotton Shirt", hsn="6205", default_price_paise=105000)
        (s.variant,) = generate_variants(s.conn, pid, ["M"], ["Blue"])
        with transaction(s.conn):
            add_movement(s.conn, s.variant, 10, "opening stock")
    return create_sale(s.conn, [{"variant_id": s.variant, "qty": qty}],
                       [{"mode": "cash", "amount_paise": 105000 * qty}], user_id=s.owner, mode="regular")


def _rule(s, hsn, ceiling):
    return next(r for r in ta.list_tax_rules(s.conn) if r["hsn_prefix"] == hsn and r["price_ceiling_paise"] == ceiling)


# ---------------------------------------------------------------- the rate table exists in a real shop

def test_a_new_shop_starts_with_the_apparel_rate_table(tmp_path):
    folder = tmp_path / "myshop"
    conn, _session, _key = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    assert len(load_tax_rules(conn)) == len(DEFAULT_TAX_RULES) == 6
    conn.close()


def test_a_shop_made_before_the_fix_gets_the_table_when_someone_signs_in(tmp_path):
    folder = tmp_path / "myshop"
    conn, _session, _key = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    with transaction(conn):
        conn.execute("DELETE FROM tax_rules")
    conn.close()
    conn, _session = shop.open_shop(folder, "owner", "owner-password")
    assert len(load_tax_rules(conn)) == 6
    conn.close()
    conn, _session = shop.open_shop(folder, "owner", "owner-password")  # a second sign-in adds nothing
    assert len(load_tax_rules(conn)) == 6
    conn.close()


# ---------------------------------------------------------------- listing

def test_the_table_lists_status_and_use(s):
    rules = ta.list_tax_rules(s.conn)
    assert len(rules) == 6 and all(r["status"] == "active" and r["used_on_bills"] is False for r in rules)
    _bill(s)
    assert _rule(s, "62", 250000)["used_on_bills"] is True and _rule(s, "62", None)["used_on_bills"] is False
    assert ta.list_tax_rules(s.conn, on=date(2025, 1, 1))[0]["status"] == "future"


# ---------------------------------------------------------------- adding a rule

def test_a_rule_for_a_new_hsn_can_start_today(s):
    rule_id = ta.add_tax_rule(s.conn, s.owner, "64", 5, None, TODAY.isoformat(), "Footwear")
    rule = next(r for r in ta.list_tax_rules(s.conn) if r["id"] == rule_id)
    assert (rule["hsn_prefix"], rule["rate_percent"], rule["note"], rule["status"]) == ("64", 5.0, "Footwear", "active")
    assert verify_chain(s.conn) == (True, None)


@pytest.mark.parametrize("hsn, rate, ceiling, start, message", [
    ("6", 5, None, "2030-01-01", "HSN prefix"),
    ("6X", 5, None, "2030-01-01", "HSN prefix"),
    ("64", "abc", None, "2030-01-01", "must be a number"),
    ("64", 150, None, "2030-01-01", "between 0 and 100"),
    ("64", 5, 0, "2030-01-01", "price limit"),
    ("64", 5, None, "31/12/2030", "valid date"),
])
def test_wrong_rule_details_are_refused(s, hsn, rate, ceiling, start, message):
    with pytest.raises(ValueError, match=message):
        ta.add_tax_rule(s.conn, s.owner, hsn, rate, ceiling, start)
    assert len(ta.list_tax_rules(s.conn)) == 6


def test_a_second_open_rule_for_the_same_slot_is_refused(s):
    with pytest.raises(ValueError, match="already covers that date"):
        ta.add_tax_rule(s.conn, s.owner, "62", 12, 250000, (TODAY + timedelta(days=30)).isoformat())
    assert len(ta.list_tax_rules(s.conn)) == 6


def test_changing_a_rate_means_ending_the_old_rule_and_starting_a_new_one(s):
    old = _rule(s, "62", 250000)
    ta.end_tax_rule(s.conn, s.owner, old["id"], TODAY.isoformat())
    tomorrow = TODAY + timedelta(days=1)
    new_id = ta.add_tax_rule(s.conn, s.owner, "62", 12, 250000, tomorrow.isoformat())
    rules = {r["id"]: r for r in ta.list_tax_rules(s.conn)}
    assert rules[old["id"]]["valid_to"] == TODAY.isoformat() and rules[old["id"]]["rate_percent"] == 5.0
    assert rules[new_id]["status"] == "future" and len(rules) == 7  # the old rule is kept, never edited
    assert verify_chain(s.conn) == (True, None)


def test_a_rate_cannot_start_on_a_day_that_already_has_bills_for_those_codes(s):
    _bill(s)
    with pytest.raises(ValueError, match="Bills for these HSN codes already exist"):
        ta.add_tax_rule(s.conn, s.owner, "6205", 12, None, TODAY.isoformat())
    ta.add_tax_rule(s.conn, s.owner, "6205", 12, None, (TODAY + timedelta(days=1)).isoformat())  # later is fine


# ---------------------------------------------------------------- ending a rule

def test_a_rule_cannot_be_ended_before_a_bill_that_used_it(s):
    _bill(s)
    used = _rule(s, "62", 250000)
    with pytest.raises(ValueError, match="already made with this rule"):
        ta.end_tax_rule(s.conn, s.owner, used["id"], (TODAY - timedelta(days=1)).isoformat())
    assert _rule(s, "62", 250000)["valid_to"] is None
    ta.end_tax_rule(s.conn, s.owner, used["id"], TODAY.isoformat())  # the day of the bill is fine
    assert _rule(s, "62", 250000)["valid_to"] == TODAY.isoformat()


def test_ending_errors(s):
    rule = _rule(s, "61", None)
    with pytest.raises(ValueError, match="before the rule starts"):
        ta.end_tax_rule(s.conn, s.owner, rule["id"], "2025-01-01")
    with pytest.raises(ValueError, match="not found"):
        ta.end_tax_rule(s.conn, s.owner, 9999, TODAY.isoformat())
    ta.end_tax_rule(s.conn, s.owner, rule["id"], TODAY.isoformat())
    with pytest.raises(ValueError, match="already ended"):
        ta.end_tax_rule(s.conn, s.owner, rule["id"], TODAY.isoformat())


def test_a_cashier_cannot_change_rates(s):
    rule = _rule(s, "61", None)
    with pytest.raises(PermissionDenied):
        ta.add_tax_rule(s.conn, s.cashier, "64", 5, None, TODAY.isoformat())
    with pytest.raises(PermissionDenied):
        ta.end_tax_rule(s.conn, s.cashier, rule["id"], TODAY.isoformat())
    assert len(ta.list_tax_rules(s.conn)) == 6 and _rule(s, "61", None)["valid_to"] is None


# ---------------------------------------------------------------- document numbers

def test_every_series_is_listed_with_its_next_number(s):
    rows = {r["series"]: r for r in sa.list_series(s.conn)}
    fy = financial_year(TODAY)
    assert len(rows) == 11 and rows["INV"]["next_number"] == f"INV/{fy}/000001" and rows["INV"]["last_number"] == 0


def test_a_new_prefix_is_used_at_once_when_nothing_was_issued_this_year(s):
    assert sa.set_series_prefix(s.conn, s.owner, "INV", "shop") == {"changed": True, "applies_from": "now"}
    sale_id = _bill(s)
    invoice_no = s.conn.execute("SELECT invoice_no FROM sales WHERE id = ?", (sale_id,)).fetchone()[0]
    assert invoice_no == f"SHOP/{financial_year(TODAY)}/000001"
    assert verify_chain(s.conn) == (True, None)


def test_after_bills_were_issued_the_new_prefix_starts_next_financial_year(s):
    first = _bill(s)
    fy = financial_year(TODAY)
    result = sa.set_series_prefix(s.conn, s.owner, "INV", "SHOP")
    assert result["changed"] is True and result["applies_from"] == sa._next_year(fy)
    row = {r["series"]: r for r in sa.list_series(s.conn)}["INV"]
    assert row["prefix"] == "SHOP" and row["prefix_in_use"] == "INV" and row["starts_next_year"] is True
    # this year's numbers keep going with the old prefix and the same running number (never reused)
    second = _bill(s)
    names = [r[0] for r in s.conn.execute("SELECT invoice_no FROM sales WHERE id IN (?, ?) ORDER BY id", (first, second))]
    assert names == [f"INV/{fy}/000001", f"INV/{fy}/000002"]
    with transaction(s.conn):
        assert next_number(s.conn, "INV", sa._next_year(fy), "INV") == (1, f"SHOP/{sa._next_year(fy)}/000001")


def test_two_series_can_never_share_a_prefix(s):
    for taken in ("BOS", "CN", "pur"):
        with pytest.raises(ValueError, match="already uses"):
            sa.set_series_prefix(s.conn, s.owner, "INV", taken)
    _bill(s)  # INV now has a row; another series may not take its prefix either
    with pytest.raises(ValueError, match="already uses"):
        sa.set_series_prefix(s.conn, s.owner, "BOS", "INV")


@pytest.mark.parametrize("bad", ["", "   ", "A/B", "TOOLONGPFX", "IN V", "इन"])
def test_a_wrong_prefix_is_refused(s, bad):
    with pytest.raises(ValueError, match="1 to 8 letters or digits"):
        sa.set_series_prefix(s.conn, s.owner, "INV", bad)


def test_unknown_series_and_unchanged_prefix(s):
    with pytest.raises(ValueError, match="Unknown document series"):
        sa.set_series_prefix(s.conn, s.owner, "XYZ", "ABC")
    assert sa.set_series_prefix(s.conn, s.owner, "INV", "inv")["changed"] is False


def test_a_cashier_cannot_change_a_prefix(s):
    with pytest.raises(PermissionDenied):
        sa.set_series_prefix(s.conn, s.cashier, "INV", "SHOP")
    assert {r["series"]: r["prefix"] for r in sa.list_series(s.conn)}["INV"] == "INV"