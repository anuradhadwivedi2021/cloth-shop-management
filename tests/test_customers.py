import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from core.audit import verify_chain
from core.customers import (
    DuplicateCustomerError, create_customer, customer_history, erase_customer, export_customer_data,
    export_customer_data_to_file, get_customer, inactive_customers, list_access_log, normalise_phone,
    record_consent, search_customers, set_customer_active, update_customer, upcoming_birthdays,
    validate_gstin, withdraw_consent,
)
from core.permissions import PermissionDenied, set_user_permission
from core.products import create_product, generate_variants
from core.returns import get_store_credit_balance, process_return
from core.sales import create_sale, hold_bill, discard_held_bill
from core.settings import set_setting
from core.stock import add_movement
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

DAY = "2026-11-01"
VALID_GSTIN = "27AAPFU0939F1ZV"
BAD_CHECK_GSTIN = "27AAPFU0939F1ZA"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    for name, role in [("owner", "owner"), ("till", "cashier"), ("store", "stock"), ("ca", "accountant")]:
        conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)  # Rs 1,050
    (m_blue,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, m_blue, 20, "opening stock")
    yield SimpleNamespace(conn=conn, owner=1, cashier=2, stock=3, accountant=4, m_blue=m_blue, path=tmp_path)
    conn.close()


def _cust(s, phone="9876543210", **kw):
    kw.setdefault("name", "Ravi Kumar")
    kw.setdefault("consent_given", True)
    kw.setdefault("user_id", s.owner)
    return create_customer(s.conn, phone, **kw)


def _sell(s, customer_id, qty=2, paid=210000, **kw):
    kw.setdefault("on_date", DAY)
    return create_sale(
        s.conn, [{"variant_id": s.m_blue, "qty": qty}],
        [{"mode": "cash", "amount_paise": paid}] if paid else [],
        user_id=s.owner, customer_id=customer_id, **kw,
    )


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _audit_mentions(conn, text):
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE COALESCE(before_json,'') LIKE ? OR COALESCE(after_json,'') LIKE ?",
        (f"%{text}%", f"%{text}%"),
    ).fetchone()[0]


# ---------- creating a profile ----------

def test_create_needs_only_phone_and_consent_and_stores_the_consent(shop):
    cid = create_customer(shop.conn, "98765 43210", user_id=shop.cashier, consent_given=True)
    c = get_customer(shop.conn, cid, user_id=shop.cashier)
    assert c["phone"] == "9876543210" and c["name"] is None and c["is_active"] == 1
    assert c["consent_given"] == 1 and c["consent_date"] and c["consent_purpose"]


def test_a_profile_without_consent_is_refused_and_nothing_is_saved(shop):
    with pytest.raises(ValueError, match="consent"):
        create_customer(shop.conn, "9876543210", user_id=shop.owner)
    assert _count(shop.conn, "customers") == 0 and _count(shop.conn, "audit_log") == 0


def test_consent_purpose_comes_from_the_setting_or_the_caller(shop):
    set_setting(shop.conn, "customer_consent_purpose", "Bills and offers")
    a = get_customer(shop.conn, _cust(shop), user_id=shop.owner)
    b = get_customer(shop.conn, _cust(shop, phone="9111111111", consent_purpose="Alterations"), user_id=shop.owner)
    assert (a["consent_purpose"], b["consent_purpose"]) == ("Bills and offers", "Alterations")


@pytest.mark.parametrize("raw", ["+91 98765-43210", "0 9876543210", "919876543210", "(98765) 43210"])
def test_phone_numbers_are_normalised(raw):
    assert normalise_phone(raw) == "9876543210"


@pytest.mark.parametrize("raw", ["", "12345", "98765432100000", "abcdefghij"])
def test_bad_phone_numbers_are_refused(raw):
    with pytest.raises(ValueError, match="10-digit"):
        normalise_phone(raw)


def test_duplicate_phone_is_flagged_with_the_existing_profile(shop):
    first = _cust(shop)
    with pytest.raises(DuplicateCustomerError) as err:
        _cust(shop, name="Someone else")
    assert err.value.existing_id == first and _count(shop.conn, "customers") == 1
    second = _cust(shop, name="Family member", allow_duplicate_phone=True)  # shared family phone
    assert second != first


def test_gstin_is_checked_for_shape_state_and_check_character():
    assert validate_gstin(" 27aapfu0939f1zv ") == VALID_GSTIN
    for bad in (BAD_CHECK_GSTIN, "27AAPFU0939F1", "99ZZZZZ0000Z1ZZ", "00AAPFU0939F1ZV", "not a gstin"):
        with pytest.raises(ValueError, match="GSTIN"):
            validate_gstin(bad)


def test_gstin_and_birthday_are_validated_on_create(shop):
    with pytest.raises(ValueError, match="GSTIN"):
        _cust(shop, gstin=BAD_CHECK_GSTIN)
    for bad in ("2999-01-01", "2001-02-30", "25/03/2001", "13-01", "02-30", "1800-01-01"):
        with pytest.raises(ValueError, match="[Bb]irthday"):
            _cust(shop, birthday=bad)
    assert _count(shop.conn, "customers") == 0
    cid = _cust(shop, gstin=VALID_GSTIN.lower(), birthday="03-25")  # year left out on purpose
    c = get_customer(shop.conn, cid, user_id=shop.owner)
    assert (c["gstin"], c["birthday"]) == (VALID_GSTIN, "03-25")


# ---------- editing ----------

def test_update_returns_only_the_fields_that_really_changed(shop):
    cid = _cust(shop, address="Old road")
    changed = update_customer(shop.conn, cid, user_id=shop.cashier, name="Ravi Kumar", address="New road", notes="Likes XL")
    assert changed == ["address", "notes"]
    c = get_customer(shop.conn, cid, user_id=shop.owner)
    assert (c["name"], c["address"], c["notes"]) == ("Ravi Kumar", "New road", "Likes XL") and c["updated_at"]


def test_update_with_nothing_new_saves_nothing_and_logs_nothing(shop):
    cid = _cust(shop)
    before = _count(shop.conn, "audit_log")
    assert update_customer(shop.conn, cid, user_id=shop.owner, name="Ravi Kumar") == []
    assert update_customer(shop.conn, cid, user_id=shop.owner) == []
    assert _count(shop.conn, "audit_log") == before


def test_update_can_clear_optional_fields_but_not_the_phone(shop):
    cid = _cust(shop, address="Old road")
    update_customer(shop.conn, cid, user_id=shop.owner, address="  ")
    assert get_customer(shop.conn, cid, user_id=shop.owner)["address"] is None
    with pytest.raises(ValueError, match="10-digit"):
        update_customer(shop.conn, cid, user_id=shop.owner, phone="")


def test_update_rejects_unknown_fields_and_duplicate_phones(shop):
    a, b = _cust(shop), _cust(shop, phone="9111111111", name="Meena")
    with pytest.raises(ValueError, match="consent_given"):
        update_customer(shop.conn, a, user_id=shop.owner, consent_given=0)
    with pytest.raises(DuplicateCustomerError):
        update_customer(shop.conn, b, user_id=shop.owner, phone="98765 43210")
    assert update_customer(shop.conn, a, user_id=shop.owner, phone="+91 9876543210") == []  # same number, no clash with itself


def test_audit_log_never_receives_personal_details(shop):
    cid = _cust(shop, address="12 Market Road", gstin=VALID_GSTIN)
    update_customer(shop.conn, cid, user_id=shop.owner, phone="9000011111", name="Ravi K", notes="secret note")
    erase_customer(shop.conn, cid, user_id=shop.owner, reason="customer_request")
    for text in ("9876543210", "9000011111", "Ravi", "Market Road", VALID_GSTIN, "secret note"):
        assert _audit_mentions(shop.conn, text) == 0, text
    ok, _ = verify_chain(shop.conn)
    assert ok


# ---------- permissions ----------

def test_who_may_do_what(shop):
    cid = _cust(shop)
    # storekeeper has no customer access at all
    with pytest.raises(PermissionDenied):
        search_customers(shop.conn, "", user_id=shop.stock)
    with pytest.raises(PermissionDenied):
        create_customer(shop.conn, "9222222222", user_id=shop.stock, consent_given=True)
    # accountant can look but not change
    assert search_customers(shop.conn, "", user_id=shop.accountant)
    with pytest.raises(PermissionDenied):
        update_customer(shop.conn, cid, user_id=shop.accountant, name="X")
    # cashier works with customers, but export / erase / review / access log are owner-only
    assert update_customer(shop.conn, cid, user_id=shop.cashier, notes="x") == ["notes"]
    for call in (
        lambda: export_customer_data(shop.conn, cid, user_id=shop.cashier),
        lambda: erase_customer(shop.conn, cid, user_id=shop.cashier, reason="customer_request"),
        lambda: inactive_customers(shop.conn, user_id=shop.cashier),
        lambda: list_access_log(shop.conn, cid, user_id=shop.cashier),
    ):
        with pytest.raises(PermissionDenied):
            call()
    assert get_customer(shop.conn, cid, user_id=shop.owner)["name"] == "Ravi Kumar"  # nothing was erased
    # the owner can hand the data rights to someone
    set_user_permission(shop.conn, shop.owner, shop.cashier, "manage_customer_data", True)
    assert export_customer_data(shop.conn, cid, user_id=shop.cashier)["customer"]["id"] == cid


# ---------- consent, archive ----------

def test_withdrawn_consent_archives_the_profile_and_blocks_new_bills(shop):
    cid = _cust(shop)
    withdraw_consent(shop.conn, cid, user_id=shop.cashier)
    c = get_customer(shop.conn, cid, user_id=shop.owner)
    assert c["consent_given"] == 0 and c["is_active"] == 0
    with pytest.raises(ValueError):
        _sell(shop, cid)
    with pytest.raises(ValueError, match="consent"):
        set_customer_active(shop.conn, cid, True, user_id=shop.owner)
    record_consent(shop.conn, cid, user_id=shop.owner)
    assert get_customer(shop.conn, cid, user_id=shop.owner)["is_active"] == 1
    assert _sell(shop, cid)


def test_archive_and_reactivate(shop):
    cid = _cust(shop)
    set_customer_active(shop.conn, cid, False, user_id=shop.owner)
    assert search_customers(shop.conn, "Ravi", user_id=shop.owner) == []
    assert [r["id"] for r in search_customers(shop.conn, "Ravi", user_id=shop.owner, include_inactive=True)] == [cid]
    set_customer_active(shop.conn, cid, True, user_id=shop.owner)
    assert [r["id"] for r in search_customers(shop.conn, "Ravi", user_id=shop.owner)] == [cid]


def test_customers_can_never_be_hard_deleted(shop):
    cid = _cust(shop)
    with pytest.raises(sqlcipher.DatabaseError, match="never deleted"):
        shop.conn.execute("DELETE FROM customers WHERE id = ?", (cid,))


# ---------- finding ----------

def test_search_by_name_or_phone_with_wildcards_treated_as_plain_text(shop):
    a = _cust(shop, phone="9876543210", name="Asha Verma")
    b = _cust(shop, phone="9811122233", name="Bharat 100% Cotton")
    r = lambda q: [x["id"] for x in search_customers(shop.conn, q, user_id=shop.cashier)]  # noqa: E731
    assert r("asha") == [a] and r("VERMA") == [a]
    assert r("98765") == [a] and r("811122") == [b]
    assert r("+91 98111 22233") == [b] and r("09876543210") == [a]   # country code / leading 0 typed at the counter
    assert r("100%") == [b] and r("%") == [b]          # '%' is not a wildcard
    assert r("_") == [] and r("zzz") == []
    assert set(r("")) == {a, b}
    assert len(search_customers(shop.conn, "", user_id=shop.cashier, limit=1)) == 1


def test_search_returns_only_the_basics(shop):
    _cust(shop, address="12 Market Road", gstin=VALID_GSTIN)
    (hit,) = search_customers(shop.conn, "ravi", user_id=shop.cashier)
    assert set(hit) == {"id", "name", "phone", "is_active"}


def test_opening_a_profile_or_history_is_written_to_the_access_log(shop):
    cid = _cust(shop)
    search_customers(shop.conn, "ravi", user_id=shop.cashier)           # not logged
    get_customer(shop.conn, cid, user_id=shop.cashier)
    customer_history(shop.conn, cid, user_id=shop.accountant)
    log = list_access_log(shop.conn, cid, user_id=shop.owner)
    assert [(e["action"], e["user"]) for e in log] == [("view", "till"), ("history", "ca")]


def test_unknown_customer(shop):
    with pytest.raises(ValueError, match="not found"):
        get_customer(shop.conn, 999, user_id=shop.owner)


def test_access_log_is_append_only(shop):
    cid = _cust(shop)
    get_customer(shop.conn, cid, user_id=shop.owner)
    with pytest.raises(sqlcipher.DatabaseError, match="append-only"):
        shop.conn.execute("UPDATE customer_access_log SET action = 'x'")
    with pytest.raises(sqlcipher.DatabaseError, match="cannot be deleted"):
        shop.conn.execute("DELETE FROM customer_access_log")


# ---------- history ----------

def test_history_shows_bills_credit_notes_store_credit_and_dues(shop):
    set_setting(shop.conn, "credit_sales_enabled", 1)
    cid = _cust(shop)
    paid_bill = _sell(shop, cid, qty=1, paid=105000)                 # Rs 1,050 paid in full
    credit_bill = _sell(shop, cid, qty=2, paid=100000)               # Rs 2,100 bill, Rs 1,000 paid: Rs 1,100 due
    h = customer_history(shop.conn, cid, user_id=shop.owner)
    assert h["credit_sales_enabled"] is True and h["outstanding_paise"] == 110000
    assert [(b["sale_id"], b["due_paise"]) for b in h["bills"]] == [(credit_bill, 110000), (paid_bill, 0)]

    # returning one shirt from the credit bill first reduces what is owed
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ?", (credit_bill,)).fetchone()["id"]
    process_return(shop.conn, credit_bill, [{"sale_line_id": line, "qty": 1}], user_id=shop.owner,
                   reason="wrong_size", on_date=DAY)
    h = customer_history(shop.conn, cid, user_id=shop.owner)
    assert h["outstanding_paise"] == 5000 and len(h["returns"]) == 1

    # store credit from the other bill shows up as a balance
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ?", (paid_bill,)).fetchone()["id"]
    process_return(shop.conn, paid_bill, [{"sale_line_id": line, "qty": 1}], user_id=shop.owner,
                   outcome="store_credit", reason="wrong_size", on_date=DAY)
    h = customer_history(shop.conn, cid, user_id=shop.owner)
    assert h["store_credit_paise"] == 105000 == get_store_credit_balance(shop.conn, cid)


# ---------- the customer's data rights ----------

def _full_customer(s):
    cid = _cust(s, address="12 Market Road", gstin=VALID_GSTIN, birthday="2001-03-25",
                preferred_size="M", notes="Likes blue")
    _sell(s, cid, qty=1, paid=105000)
    return cid


def test_export_contains_everything_stored_and_is_logged(shop):
    cid = _full_customer(shop)
    data = export_customer_data(shop.conn, cid, user_id=shop.owner)
    assert data["customer"]["phone"] == "9876543210" and data["customer"]["address"] == "12 Market Road"
    assert data["customer"]["consent_purpose"]
    (bill,) = data["bills"]
    assert bill["total_paise"] == 105000 and bill["payments"][0]["mode"] == "cash"
    assert [e["action"] for e in data["access_log"]] == ["export"]    # this export itself is on the record
    json.dumps(data)                                                    # plain data, can be written as JSON
    assert _audit_mentions(shop.conn, "9876543210") == 0


def test_export_to_file_writes_json_without_leftovers(shop):
    cid = _full_customer(shop)
    target = shop.path / "ravi.json"
    assert export_customer_data_to_file(shop.conn, cid, str(target), user_id=shop.owner) == str(target)
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["customer"]["id"] == cid and saved["bills"]
    assert list(shop.path.glob("*.part")) == []


def test_erase_removes_personal_data_and_keeps_the_money_trail(shop):
    cid = _cust(shop, address="12 Market Road", birthday="2001-03-25", preferred_size="M", notes="Likes blue")
    sale = _sell(shop, cid, qty=1, paid=105000)
    result = erase_customer(shop.conn, cid, user_id=shop.owner, reason="customer_request")
    assert result["kept_for_tax"] == [] and "name" in result["erased"]
    row = shop.conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    assert (row["name"], row["phone"], row["address"], row["gstin"], row["birthday"]) == ("Erased customer", "", None, None, None)
    assert (row["preferred_size"], row["notes"], row["consent_given"], row["consent_purpose"]) == (None, None, 0, None)
    assert row["is_active"] == 0 and row["anonymised_at"]
    # the bill still exists and still points to the (now anonymous) customer
    assert shop.conn.execute("SELECT customer_id, total_paise FROM sales WHERE id = ?", (sale,)).fetchone()[:] == (cid, 105000)
    assert search_customers(shop.conn, "Erased", user_id=shop.owner, include_inactive=True) == []
    assert [e["action"] for e in list_access_log(shop.conn, cid, user_id=shop.owner)] == ["erase"]
    assert _audit_mentions(shop.conn, "Ravi") == 0


def test_erase_keeps_name_and_gstin_when_a_tax_invoice_needs_them(shop):
    cid = _cust(shop, gstin=VALID_GSTIN)
    _sell(shop, cid, qty=1, paid=105000)
    result = erase_customer(shop.conn, cid, user_id=shop.owner, reason="customer_request")
    assert result["kept_for_tax"] == ["name", "gstin"]
    row = shop.conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    assert (row["name"], row["gstin"], row["phone"]) == ("Ravi Kumar", VALID_GSTIN, "")
    # a GSTIN without any bill has no tax record to protect, so it goes too
    lonely = _cust(shop, phone="9111111111", gstin=VALID_GSTIN)
    assert erase_customer(shop.conn, lonely, user_id=shop.owner, reason="customer_request")["kept_for_tax"] == []
    assert shop.conn.execute("SELECT gstin FROM customers WHERE id = ?", (lonely,)).fetchone()[0] is None


def test_erase_is_refused_while_money_or_a_held_bill_depends_on_the_customer(shop):
    set_setting(shop.conn, "credit_sales_enabled", 1)
    owes = _cust(shop)
    _sell(shop, owes, qty=2, paid=100000)  # Rs 1,100 due
    with pytest.raises(ValueError, match="still owes Rs 1,100.00"):
        erase_customer(shop.conn, owes, user_id=shop.owner, reason="customer_request")

    credit = _cust(shop, phone="9111111111")
    paid = _sell(shop, credit, qty=1, paid=105000)
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ?", (paid,)).fetchone()["id"]
    process_return(shop.conn, paid, [{"sale_line_id": line, "qty": 1}], user_id=shop.owner,
                   outcome="store_credit", reason="wrong_size", on_date=DAY)
    with pytest.raises(ValueError, match="store credit of Rs 1,050.00"):
        erase_customer(shop.conn, credit, user_id=shop.owner, reason="customer_request")

    held = _cust(shop, phone="9222222222")
    slot = hold_bill(shop.conn, [{"variant_id": shop.m_blue, "qty": 1}], user_id=shop.owner, customer_id=held)
    with pytest.raises(ValueError, match="held bill"):
        erase_customer(shop.conn, held, user_id=shop.owner, reason="customer_request")
    discard_held_bill(shop.conn, slot)
    erase_customer(shop.conn, held, user_id=shop.owner, reason="customer_request")  # now it works
    for cid in (owes, credit):  # the refused erasures left both profiles untouched
        assert get_customer(shop.conn, cid, user_id=shop.owner)["phone"] != ""


def test_erase_needs_a_known_reason_and_works_only_once(shop):
    cid = _cust(shop)
    with pytest.raises(ValueError, match="Reason must be one of"):
        erase_customer(shop.conn, cid, user_id=shop.owner, reason="Ravi called me today")
    erase_customer(shop.conn, cid, user_id=shop.owner, reason="consent_withdrawn")
    with pytest.raises(ValueError, match="already erased"):
        erase_customer(shop.conn, cid, user_id=shop.owner, reason="consent_withdrawn")


def test_an_erased_customer_cannot_be_edited_billed_or_brought_back(shop):
    cid = _cust(shop)
    erase_customer(shop.conn, cid, user_id=shop.owner, reason="customer_request")
    for call in (
        lambda: update_customer(shop.conn, cid, user_id=shop.owner, name="Back again"),
        lambda: set_customer_active(shop.conn, cid, True, user_id=shop.owner),
        lambda: record_consent(shop.conn, cid, user_id=shop.owner),
        lambda: withdraw_consent(shop.conn, cid, user_id=shop.owner),
        lambda: _sell(shop, cid),
    ):
        with pytest.raises(ValueError):
            call()
    # the phone number is free to be used for a new profile
    assert _cust(shop) != cid


# ---------- reviews and lists ----------

def test_inactive_customers_need_no_activity_for_the_whole_period(shop):
    today = date.today()
    old = _cust(shop, phone="9111111111", name="Old")
    still_buying = _cust(shop, phone="9222222222", name="Buyer")
    new = _cust(shop, phone="9333333333", name="New")
    long_ago = f"{today.year - 3}-01-01 10:00:00"
    shop.conn.execute("UPDATE customers SET created_at = ? WHERE id IN (?, ?)", (long_ago, old, still_buying))
    _sell(shop, still_buying, qty=1, paid=105000)                       # a recent bill counts as activity

    rows = inactive_customers(shop.conn, user_id=shop.owner, months=12)
    assert [r["id"] for r in rows] == [old] and rows[0]["last_activity"] == long_ago

    # looking ahead: with no new activity everybody goes quiet; the default period comes from the setting
    later = today + timedelta(days=400)
    assert {r["id"] for r in inactive_customers(shop.conn, user_id=shop.owner, months=12, today=later)} == {old, still_buying, new}
    set_setting(shop.conn, "customer_retention_months", 60)
    assert inactive_customers(shop.conn, user_id=shop.owner, today=later) == []
    with pytest.raises(ValueError, match="at least 1"):
        inactive_customers(shop.conn, user_id=shop.owner, months=0)


def test_inactive_review_changes_nothing_and_skips_erased_customers(shop):
    old = _cust(shop, phone="9111111111")
    gone = _cust(shop, phone="9222222222")
    shop.conn.execute("UPDATE customers SET created_at = '2000-01-01 10:00:00'")
    erase_customer(shop.conn, gone, user_id=shop.owner, reason="retention_review")
    assert [r["id"] for r in inactive_customers(shop.conn, user_id=shop.owner, months=1)] == [old]
    assert get_customer(shop.conn, old, user_id=shop.owner)["is_active"] == 1


def test_upcoming_birthdays(shop):
    soon = _cust(shop, phone="9111111111", name="Soon", birthday="1990-11-04")
    tomorrow = _cust(shop, phone="9222222222", name="Tomorrow", birthday="11-02")
    _cust(shop, phone="9333333333", name="Later", birthday="12-25")
    _cust(shop, phone="9444444444", name="Passed", birthday="2000-10-30")
    _cust(shop, phone="9555555555", name="None")
    archived = _cust(shop, phone="9666666666", name="Archived", birthday="11-03")
    set_customer_active(shop.conn, archived, False, user_id=shop.owner)

    rows = upcoming_birthdays(shop.conn, user_id=shop.cashier, days=7, today=date(2026, 11, 1))
    assert [(r["id"], r["days_until"], r["birthday"]) for r in rows] == [(tomorrow, 1, "11-02"), (soon, 3, "11-04")]


def test_birthdays_across_new_year_and_29_february(shop):
    ny = _cust(shop, phone="9111111111", name="NewYear", birthday="01-02")
    leap = _cust(shop, phone="9222222222", name="Leap", birthday="2000-02-29")
    rows = upcoming_birthdays(shop.conn, user_id=shop.owner, days=7, today=date(2026, 12, 28))
    assert [(r["id"], r["days_until"]) for r in rows] == [(ny, 5)]
    # 2027 has no 29 Feb: celebrated on the 28th
    rows = upcoming_birthdays(shop.conn, user_id=shop.owner, days=3, today=date(2027, 2, 26))
    assert [(r["id"], r["days_until"]) for r in rows] == [(leap, 2)]
    # 2028 does
    rows = upcoming_birthdays(shop.conn, user_id=shop.owner, days=3, today=date(2028, 2, 27))
    assert [(r["id"], r["days_until"]) for r in rows] == [(leap, 2)]


def test_whole_flow_keeps_the_audit_chain_valid(shop):
    cid = _full_customer(shop)
    update_customer(shop.conn, cid, user_id=shop.cashier, notes="more")
    withdraw_consent(shop.conn, cid, user_id=shop.cashier)
    export_customer_data(shop.conn, cid, user_id=shop.owner)
    erase_customer(shop.conn, cid, user_id=shop.owner, reason="consent_withdrawn")
    ok, bad = verify_chain(shop.conn)
    assert ok and bad is None


def test_anniversary_is_on_the_call_list(shop):
    cid = create_customer(shop.conn, "9000000011", user_id=shop.owner, name="Meena", consent_given=True,
                          birthday="03-25", anniversary="2015-11-03")
    rows = upcoming_birthdays(shop.conn, user_id=shop.owner, days=7, today=date(2026, 11, 1))
    assert [(r["id"], r["occasion"], r["days_until"]) for r in rows] == [(cid, "anniversary", 2)]
    update_customer(shop.conn, cid, user_id=shop.owner, anniversary="11-05")
    assert upcoming_birthdays(shop.conn, user_id=shop.owner, days=7, today=date(2026, 11, 1))[0]["birthday"] == "11-05"
    with pytest.raises(ValueError, match="Anniversary"):
        update_customer(shop.conn, cid, user_id=shop.owner, anniversary="13-45")
