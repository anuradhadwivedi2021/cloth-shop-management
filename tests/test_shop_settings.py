import string
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from core import shop_settings as ss
from core.audit import verify_chain
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.returns import _window_days
from core.sales import create_sale, get_sale
from core.session import auto_lock_minutes
from core.settings import get_bool, get_setting
from core.stock import add_movement
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction
from db.schema import MIGRATIONS

CHARS = string.digits + string.ascii_uppercase


def make_gstin(state, pan="AAPFU0939F", entity="1"):
    """A GSTIN with a correct check character (same rule as the app uses)."""
    body = f"{state}{pan}{entity}Z"
    total = 0
    for position, char in enumerate(body):
        product = CHARS.index(char) * (2 if position % 2 else 1)
        total += product // 36 + product % 36
    return body + CHARS[(36 - total % 36) % 36]


MH = make_gstin("27")   # Maharashtra
KA = make_gstin("29", pan="BBQPK1234L")   # Karnataka
DL = make_gstin("07", pan="CCRPD5678M")   # Delhi


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    for name, role in (("owner", "owner"), ("till", "cashier"), ("ca", "accountant")):
        conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    yield SimpleNamespace(conn=conn, owner=1, cashier=2, accountant=3)
    conn.close()


def _setup(s, **extra):
    fields = dict(name="Anu Cloth House", address="MG Road, Pune", phone="9876543210", gstin=MH)
    fields.update(extra)
    return ss.update_shop_profile(s.conn, s.owner, now=datetime(2026, 4, 1, 10, 0, 0), **fields)


# ---------------------------------------------------------------- migration

def test_migration_9_is_applied(s):
    assert 9 in MIGRATIONS
    versions = [r[0] for r in s.conn.execute("SELECT version FROM schema_version")]
    assert 9 in versions


# ---------------------------------------------------------------- profile

def test_fresh_shop_is_not_set_up(s):
    profile = ss.get_shop_profile(s.conn)
    assert profile["is_set_up"] is False and profile["name"] is None
    assert ss.billing_problems(s.conn, "regular") == ["Shop details are not set up yet."]


def test_set_up_profile_takes_state_from_the_gstin(s):
    changed = _setup(s)
    assert set(changed) == {"name", "address", "phone", "gstin", "state_code"}
    profile = ss.get_shop_profile(s.conn)
    assert profile["name"] == "Anu Cloth House" and profile["gstin"] == MH
    assert profile["state_code"] == "27" and profile["state_name"] == "Maharashtra"
    assert profile["tax_scheme"] == "regular" and profile["is_set_up"] is True
    assert s.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'shop_profile_changed'").fetchone()[0] == 1
    assert verify_chain(s.conn) == (True, None)


def test_change_adds_a_version_and_old_bills_keep_the_old_details(s):
    _setup(s)
    ss.update_shop_profile(s.conn, s.owner, now=datetime(2026, 9, 10, 9, 0, 0), address="FC Road, Pune", phone="9000000000")
    assert ss.get_shop_profile(s.conn)["address"] == "FC Road, Pune"
    assert ss.get_shop_profile(s.conn, "2026-06-15")["address"] == "MG Road, Pune"  # reprint of an older bill
    assert ss.get_shop_profile(s.conn, "2026-09-10")["address"] == "FC Road, Pune"
    assert ss.get_shop_profile(s.conn, "2025-01-01")["address"] == "MG Road, Pune"  # before setup: first version
    assert s.conn.execute("SELECT COUNT(*) FROM shop_profile_versions").fetchone()[0] == 2


def test_no_change_means_no_new_version(s):
    _setup(s)
    assert ss.update_shop_profile(s.conn, s.owner, address="MG Road, Pune", gstin=MH.lower()) == []
    assert s.conn.execute("SELECT COUNT(*) FROM shop_profile_versions").fetchone()[0] == 1


def test_profile_history_cannot_be_edited_or_deleted(s):
    _setup(s)
    with pytest.raises(sqlcipher.Error):
        s.conn.execute("UPDATE shop_profile_versions SET name = 'Fake'")
    with pytest.raises(sqlcipher.Error):
        s.conn.execute("DELETE FROM shop_profile_versions")


def test_profile_validation(s):
    with pytest.raises(ValueError, match="name is required"):
        ss.update_shop_profile(s.conn, s.owner, address="x")
    with pytest.raises(ValueError, match="GSTIN"):
        _setup(s, gstin="27AAPFU0939F1ZX")  # wrong check character
    with pytest.raises(ValueError, match="needs its GSTIN"):
        ss.update_shop_profile(s.conn, s.owner, name="Shop")  # regular scheme, no GSTIN
    with pytest.raises(ValueError, match="does not match"):
        _setup(s, state_code="29")
    with pytest.raises(ValueError, match="Phone"):
        _setup(s, phone="12")
    with pytest.raises(ValueError, match="Email"):
        _setup(s, email="not-an-email")
    with pytest.raises(ValueError, match="UPI"):
        _setup(s, upi_id="no-at-sign")
    with pytest.raises(ValueError, match="Tax scheme"):
        _setup(s, tax_scheme="gold")
    with pytest.raises(ValueError, match="Unknown shop detail"):
        ss.update_shop_profile(s.conn, s.owner, colour="red")
    assert ss.get_shop_profile(s.conn)["is_set_up"] is False  # nothing half-saved


def test_valid_upi_and_email_are_accepted(s):
    _setup(s, upi_id="anucloth@okbank", email="shop@example.com")
    profile = ss.get_shop_profile(s.conn)
    assert profile["upi_id"] == "anucloth@okbank" and profile["email"] == "shop@example.com"


def test_only_the_owner_side_can_change_shop_details(s):
    with pytest.raises(PermissionDenied):
        ss.update_shop_profile(s.conn, s.cashier, name="Hacked", gstin=MH)
    with pytest.raises(PermissionDenied):
        ss.update_shop_profile(s.conn, s.accountant, name="Hacked", gstin=MH)
    assert ss.get_shop_profile(s.conn)["is_set_up"] is False


def test_unregistered_shop_can_work_without_gstin(s):
    ss.update_shop_profile(s.conn, s.owner, name="Small Shop", tax_scheme="unregistered", state_code="27")
    assert ss.get_shop_profile(s.conn)["state_code"] == "27"
    assert ss.billing_problems(s.conn, "unregistered") == []
    problems = ss.billing_problems(s.conn, "regular")
    assert any("GSTIN" in p for p in problems) and any("unregistered" in p for p in problems)


# ---------------------------------------------------------------- billing readiness

def test_gst_shop_can_issue_both_a_tax_invoice_and_a_simple_bill(s):
    _setup(s)
    assert ss.billing_problems(s.conn, "regular") == []
    assert ss.billing_problems(s.conn, "unregistered") == []  # a bill without GST is always allowed
    assert any("regular GST" in p for p in ss.billing_problems(s.conn, "composition"))


def test_composition_shop_cannot_issue_tax_invoice(s):
    _setup(s, tax_scheme="composition")
    assert ss.billing_problems(s.conn, "composition") == []
    assert any("composition" in p for p in ss.billing_problems(s.conn, "regular"))


# ---------------------------------------------------------------- place of supply

def test_walk_in_and_same_state_customers_get_cgst_sgst(s):
    _setup(s)
    walk_in = ss.place_of_supply(s.conn)
    assert walk_in["intra_state"] is True and walk_in["label"] == "CGST + SGST" and walk_in["warning"] is None
    same = ss.place_of_supply(s.conn, customer_gstin=make_gstin("27", pan="DDSPM1111N"))
    assert same["intra_state"] is True and same["customer_state_code"] == "27"


def test_other_state_customers_get_igst(s):
    _setup(s)
    by_gstin = ss.place_of_supply(s.conn, customer_gstin=KA)
    assert by_gstin["intra_state"] is False and by_gstin["label"] == "IGST" and by_gstin["customer_state_code"] == "29"
    by_state = ss.place_of_supply(s.conn, customer_state_code="07")
    assert by_state["intra_state"] is False and by_state["source"] == "state chosen for the bill"
    assert ss.place_of_supply(s.conn, customer_state_code="27")["intra_state"] is True


def test_place_of_supply_checks_input_and_warns_without_shop_state(s):
    with pytest.raises(ValueError):
        ss.place_of_supply(s.conn, customer_gstin="bad")
    with pytest.raises(ValueError, match="State code"):
        ss.place_of_supply(s.conn, customer_state_code="55")
    out = ss.place_of_supply(s.conn, customer_gstin=DL)  # shop not set up
    assert out["intra_state"] is True and "state is not set" in out["warning"]


def test_igst_bill_uses_the_place_of_supply_result(s):
    _setup(s)
    ensure_default_tax_rules(s.conn)
    pid = create_product(s.conn, "Cotton Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(s.conn, pid, ["M"], ["Blue"])
    with transaction(s.conn):
        add_movement(s.conn, variant, 10, "opening stock")

    def bill(**customer):
        pos = ss.place_of_supply(s.conn, **customer)
        sale_id = create_sale(
            s.conn, [{"variant_id": variant, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
            user_id=s.owner, on_date="2026-11-01", mode="regular", intra_state=pos["intra_state"],
        )
        return get_sale(s.conn, sale_id)["lines"][0]

    local, other = bill(), bill(customer_gstin=KA)
    assert local["igst_paise"] == 0 and local["cgst_paise"] > 0 and local["cgst_paise"] + local["sgst_paise"] == local["tax_paise"]
    assert other["cgst_paise"] == other["sgst_paise"] == 0 and other["igst_paise"] == other["tax_paise"] > 0


# ---------------------------------------------------------------- preferences

def test_preferences_show_defaults_for_the_settings_screen(s):
    prefs = ss.get_preferences(s.conn)
    assert prefs["credit_sales_enabled"]["value"] is False
    assert prefs["auto_lock_minutes"]["value"] == 5
    assert prefs["backup_reminder_days"]["value"] == 7
    assert prefs["return_window_days"]["value"] is None
    assert prefs["bill_language"]["value"] == "en" and prefs["bill_language"]["choices"] == ("en", "hi", "mr")
    assert prefs["default_bill_mode"]["value"] == "regular"
    assert all(p["is_default"] for p in prefs.values())


def test_update_preferences_saves_typed_values_and_logs(s):
    changed = ss.update_preferences(s.conn, s.owner, {
        "credit_sales_enabled": True, "return_window_days": "7", "bill_language": "MR",
        "auto_lock_minutes": 10, "final_sale_categories": " Innerwear, sarees ,innerwear ",
    })
    assert changed == ["auto_lock_minutes", "bill_language", "credit_sales_enabled",
                       "final_sale_categories", "return_window_days"]
    prefs = ss.get_preferences(s.conn)
    assert prefs["credit_sales_enabled"]["value"] is True and prefs["credit_sales_enabled"]["is_default"] is False
    assert prefs["return_window_days"]["value"] == 7 and prefs["bill_language"]["value"] == "mr"
    entry = s.conn.execute("SELECT before_json, after_json FROM audit_log WHERE action = 'settings_changed'").fetchone()
    assert '"credit_sales_enabled":"1"' in entry["after_json"].replace(" ", "")
    assert ss.update_preferences(s.conn, s.owner, {"credit_sales_enabled": True}) == []  # no change, no log
    assert s.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'settings_changed'").fetchone()[0] == 1


def test_existing_code_reads_the_new_preferences(s):
    ss.update_preferences(s.conn, s.owner, {
        "credit_sales_enabled": True, "allow_negative_stock": True, "return_window_days": 7, "auto_lock_minutes": 15,
    })
    assert get_bool(s.conn, "credit_sales_enabled") is True
    assert get_bool(s.conn, "allow_negative_stock") is True
    assert _window_days(s.conn) == 7
    assert auto_lock_minutes(s.conn) == 15
    ss.update_preferences(s.conn, s.owner, {"credit_sales_enabled": False, "return_window_days": ""})
    assert get_bool(s.conn, "credit_sales_enabled") is False
    assert _window_days(s.conn) is None  # empty = no time limit
    assert get_setting(s.conn, "return_window_days") == ""


def test_final_sale_categories_are_cleaned(s):
    ss.update_preferences(s.conn, s.owner, {"final_sale_categories": " Innerwear, sarees ,Innerwear ,, "})
    assert get_setting(s.conn, "final_sale_categories") == "Innerwear, sarees"


def test_bad_preferences_are_refused_and_nothing_is_saved(s):
    with pytest.raises(ValueError, match="between 1 and 240"):
        ss.update_preferences(s.conn, s.owner, {"credit_sales_enabled": True, "auto_lock_minutes": 0})
    assert get_setting(s.conn, "credit_sales_enabled") is None  # all or nothing
    with pytest.raises(ValueError, match="whole number"):
        ss.update_preferences(s.conn, s.owner, {"auto_lock_minutes": "ten"})
    with pytest.raises(ValueError, match="choose one of"):
        ss.update_preferences(s.conn, s.owner, {"bill_language": "fr"})
    with pytest.raises(ValueError, match="choose one of"):
        ss.update_preferences(s.conn, s.owner, {"bill_paper": "a3"})
    with pytest.raises(ValueError, match="between 0 and 365"):
        ss.update_preferences(s.conn, s.owner, {"return_window_days": 1000})
    with pytest.raises(ValueError, match="on or off"):
        ss.update_preferences(s.conn, s.owner, {"credit_sales_enabled": "maybe"})
    with pytest.raises(ValueError, match="too long"):
        ss.update_preferences(s.conn, s.owner, {"bill_footer": "x" * 301})
    with pytest.raises(ValueError, match="a number is required"):
        ss.update_preferences(s.conn, s.owner, {"auto_lock_minutes": ""})
    with pytest.raises(ValueError, match="Unknown setting"):
        ss.update_preferences(s.conn, s.owner, {"last_backup_at": "2026-01-01"})  # system values are not editable here


def test_only_people_with_permission_change_settings(s):
    with pytest.raises(PermissionDenied):
        ss.update_preferences(s.conn, s.cashier, {"credit_sales_enabled": True})
    with pytest.raises(PermissionDenied):
        ss.update_preferences(s.conn, s.accountant, {"bill_language": "hi"})
    assert get_setting(s.conn, "credit_sales_enabled") is None


def test_changing_settings_does_not_touch_old_bills(s):
    _setup(s)
    ensure_default_tax_rules(s.conn)
    pid = create_product(s.conn, "Cotton Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(s.conn, pid, ["M"], ["Blue"])
    with transaction(s.conn):
        add_movement(s.conn, variant, 5, "opening stock")
    sale_id = create_sale(s.conn, [{"variant_id": variant, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                          user_id=s.owner, on_date=date(2026, 11, 1).isoformat(), mode="regular")
    before = get_sale(s.conn, sale_id)
    ss.update_preferences(s.conn, s.owner, {"bill_language": "hi", "default_bill_mode": "unregistered"})
    ss.update_shop_profile(s.conn, s.owner, name="Renamed Shop")
    assert get_sale(s.conn, sale_id) == before
