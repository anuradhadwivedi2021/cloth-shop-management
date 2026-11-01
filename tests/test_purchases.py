import pytest

import core.purchases as purchases_module
from core.numbering import financial_year
from core.products import create_product, generate_variants
from core.purchases import (
    PurchaseStateError,
    calculate_purchase,
    cancel_draft_purchase,
    confirm_purchase,
    create_draft_purchase,
    create_supplier,
    find_duplicate_purchase,
    get_purchase,
)
from core.stock import get_stock
from db.connection import open_db


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "shop.db", "test-password")
    yield c
    c.close()


@pytest.fixture
def supplier(conn):
    return create_supplier(conn, "Ravi Textiles", phone="9999999999")


@pytest.fixture
def variants(conn):
    """Three variants of one shirt: M-Blue, L-Blue, M-Red."""
    pid = create_product(conn, "Shirt")
    ids = generate_variants(conn, pid, ["M", "L"], ["Blue"])
    ids += generate_variants(conn, pid, ["M"], ["Red"])
    return ids


def _lines(variant_ids, qty=10, cost=50000, rate=5):
    return [
        {"variant_id": v, "qty": qty, "unit_cost_paise": cost, "tax_rate_percent": rate}
        for v in variant_ids
    ]


def _draft(conn, supplier, variants, invoice_no="INV-1", invoice_date="2026-11-01", **kw):
    return create_draft_purchase(conn, supplier, invoice_no, invoice_date, _lines(variants), **kw)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------- calculation ----------

def test_calculation_totals_and_per_line_tax():
    calc = calculate_purchase(_lines([1, 2], qty=10, cost=50000, rate=5))
    assert calc["subtotal_paise"] == 1_000_000
    assert calc["tax_paise"] == 50_000
    assert calc["lines"][0]["tax_paise"] == 25_000
    assert calc["lines"][0]["line_total_paise"] == 525_000
    assert calc["total_paise"] == 1_050_000
    assert calc["round_off_paise"] == 0


def test_tax_rounds_half_up_per_line_and_total_rounds_to_rupee():
    # 3 x 33333 paise at 5% = 4999.95 paise, rounded half-up to 5000 per line
    calc = calculate_purchase(_lines([1], qty=3, cost=33333, rate=5))
    assert calc["lines"][0]["tax_paise"] == 5000  # 4999.95 rounds to 5000
    assert calc["total_paise"] == 105_000  # 99999 + 5000 = 104999 -> 105000
    assert calc["round_off_paise"] == 1


def test_round_off_half_goes_up():
    calc = calculate_purchase(_lines([1], qty=1, cost=105050, rate=0))
    assert calc["total_paise"] == 105_100
    assert calc["round_off_paise"] == 50


def test_discount_and_freight_change_the_total():
    calc = calculate_purchase(
        _lines([1], qty=10, cost=50000, rate=0), discount_paise=20_000, freight_paise=5_000
    )
    assert calc["total_paise"] == 500_000 - 20_000 + 5_000


@pytest.mark.parametrize(
    "bad",
    [
        {"lines": []},
        {"lines": _lines([1], qty=0)},
        {"lines": _lines([1], qty=-2)},
        {"lines": _lines([1], qty=1.5)},
        {"lines": _lines([1], cost=-1)},
        {"lines": _lines([1], rate=-5)},
        {"lines": _lines([1]), "discount_paise": -1},
        {"lines": _lines([1]), "freight_paise": -1},
        {"lines": _lines([1], qty=1, cost=1000), "discount_paise": 999_999},
    ],
)
def test_invalid_calculation_inputs_are_rejected(bad):
    lines = bad.pop("lines")
    with pytest.raises(ValueError):
        calculate_purchase(lines, **bad)


# ---------- supplier ----------

def test_supplier_name_is_required(conn):
    with pytest.raises(ValueError):
        create_supplier(conn, "   ")


# ---------- draft ----------

def test_draft_purchase_does_not_change_stock(conn, supplier, variants):
    pid = _draft(conn, supplier, variants)
    purchase, lines = get_purchase(conn, pid)
    assert purchase["status"] == "draft"
    assert purchase["voucher_no"] is None
    assert len(lines) == 3
    assert all(get_stock(conn, v) == 0 for v in variants)
    assert _count(conn, "stock_movements") == 0


def test_draft_stores_calculated_totals(conn, supplier, variants):
    pid = _draft(conn, supplier, variants, paid_paise=100_000)
    purchase, _ = get_purchase(conn, pid)
    assert purchase["subtotal_paise"] == 1_500_000
    assert purchase["tax_paise"] == 75_000
    assert purchase["total_paise"] == 1_575_000
    assert purchase["paid_paise"] == 100_000
    assert purchase["due_paise"] == 1_475_000


def test_draft_needs_invoice_number(conn, supplier, variants):
    with pytest.raises(ValueError):
        _draft(conn, supplier, variants, invoice_no="  ")


def test_draft_rejects_bad_date(conn, supplier, variants):
    with pytest.raises(ValueError):
        _draft(conn, supplier, variants, invoice_date="01/11/2026")


def test_amount_paid_cannot_exceed_total(conn, supplier, variants):
    with pytest.raises(ValueError):
        _draft(conn, supplier, variants, paid_paise=99_999_999)
    with pytest.raises(ValueError):
        _draft(conn, supplier, variants, paid_paise=-1)


def test_unknown_supplier_or_variant_saves_nothing(conn, supplier, variants):
    with pytest.raises(ValueError):
        _draft(conn, 9999, variants)
    with pytest.raises(ValueError):
        create_draft_purchase(conn, supplier, "X", "2026-11-01", _lines([variants[0], 9999]))
    assert _count(conn, "purchases") == 0
    assert _count(conn, "purchase_lines") == 0


# ---------- confirm ----------

def test_confirm_adds_stock_to_each_exact_variant(conn, supplier, variants):
    lines = [
        {"variant_id": variants[0], "qty": 5, "unit_cost_paise": 40000, "tax_rate_percent": 5},
        {"variant_id": variants[1], "qty": 8, "unit_cost_paise": 40000, "tax_rate_percent": 5},
        {"variant_id": variants[2], "qty": 3, "unit_cost_paise": 45000, "tax_rate_percent": 5},
    ]
    pid = create_draft_purchase(conn, supplier, "INV-9", "2026-11-01", lines)
    confirm_purchase(conn, pid)
    assert [get_stock(conn, v) for v in variants] == [5, 8, 3]


def test_confirm_sets_status_voucher_and_links_movements(conn, supplier, variants):
    pid = _draft(conn, supplier, variants)
    voucher = confirm_purchase(conn, pid, user_id=None)
    purchase, _ = get_purchase(conn, pid)
    assert purchase["status"] == "confirmed"
    assert purchase["voucher_no"] == voucher == f"PUR/{financial_year('2026-11-01')}/000001"
    assert purchase["confirmed_at"] is not None
    moves = conn.execute("SELECT doc_type, doc_id, reason FROM stock_movements").fetchall()
    assert len(moves) == 3
    assert all(m["doc_type"] == "purchase" and m["doc_id"] == pid for m in moves)


def test_confirming_twice_is_refused_and_stock_added_once(conn, supplier, variants):
    pid = _draft(conn, supplier, variants)
    confirm_purchase(conn, pid)
    with pytest.raises(PurchaseStateError):
        confirm_purchase(conn, pid)
    assert [get_stock(conn, v) for v in variants] == [10, 10, 10]
    assert _count(conn, "stock_movements") == 3


def test_voucher_numbers_are_sequential(conn, supplier, variants):
    first = confirm_purchase(conn, _draft(conn, supplier, variants, invoice_no="A"))
    second = confirm_purchase(conn, _draft(conn, supplier, variants, invoice_no="B"))
    assert first.endswith("000001")
    assert second.endswith("000002")


def test_confirm_unknown_purchase_is_rejected(conn):
    with pytest.raises(ValueError):
        confirm_purchase(conn, 12345)


def test_payment_is_recorded_on_confirm(conn, supplier, variants):
    pid = _draft(conn, supplier, variants, paid_paise=200_000)
    confirm_purchase(conn, pid, payment_mode="upi")
    pay = conn.execute("SELECT * FROM payments").fetchall()
    assert len(pay) == 1
    assert pay[0]["document_type"] == "purchase"
    assert pay[0]["document_id"] == pid
    assert pay[0]["mode"] == "upi"
    assert pay[0]["amount_paise"] == 200_000


def test_no_payment_row_when_nothing_paid(conn, supplier, variants):
    confirm_purchase(conn, _draft(conn, supplier, variants))
    assert _count(conn, "payments") == 0


def test_failure_during_confirm_rolls_everything_back(conn, supplier, variants, monkeypatch):
    pid = _draft(conn, supplier, variants, paid_paise=100_000)
    real = purchases_module.add_movement
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr(purchases_module, "add_movement", flaky)
    with pytest.raises(RuntimeError):
        confirm_purchase(conn, pid)

    purchase, _ = get_purchase(conn, pid)
    assert purchase["status"] == "draft"
    assert purchase["voucher_no"] is None
    assert [get_stock(conn, v) for v in variants] == [0, 0, 0]
    assert _count(conn, "payments") == 0
    assert _count(conn, "number_series") == 0  # voucher number was released too

    # after the failure, a retry works and uses voucher number 1
    monkeypatch.setattr(purchases_module, "add_movement", real)
    assert confirm_purchase(conn, pid).endswith("000001")


# ---------- cancel ----------

def test_cancelled_draft_cannot_be_confirmed(conn, supplier, variants):
    pid = _draft(conn, supplier, variants)
    cancel_draft_purchase(conn, pid)
    assert get_purchase(conn, pid)[0]["status"] == "cancelled"
    with pytest.raises(PurchaseStateError):
        confirm_purchase(conn, pid)
    assert [get_stock(conn, v) for v in variants] == [0, 0, 0]


def test_confirmed_purchase_cannot_be_cancelled(conn, supplier, variants):
    pid = _draft(conn, supplier, variants)
    confirm_purchase(conn, pid)
    with pytest.raises(PurchaseStateError):
        cancel_draft_purchase(conn, pid)
    assert [get_stock(conn, v) for v in variants] == [10, 10, 10]


# ---------- duplicate supplier invoice warning ----------

def test_duplicate_invoice_is_found_ignoring_case_and_spaces(conn, supplier, variants):
    pid = _draft(conn, supplier, variants, invoice_no="AB-100")
    found = find_duplicate_purchase(conn, supplier, "  ab-100 ", "2026-11-05")
    assert [r["id"] for r in found] == [pid]


def test_same_invoice_from_another_supplier_is_not_a_duplicate(conn, supplier, variants):
    _draft(conn, supplier, variants, invoice_no="AB-100")
    other = create_supplier(conn, "Mehta Garments")
    assert find_duplicate_purchase(conn, other, "AB-100", "2026-11-01") == []


def test_same_invoice_in_next_financial_year_is_not_a_duplicate(conn, supplier, variants):
    _draft(conn, supplier, variants, invoice_no="AB-100", invoice_date="2026-11-01")
    assert find_duplicate_purchase(conn, supplier, "AB-100", "2027-04-01") == []


def test_cancelled_purchase_is_ignored_by_duplicate_check(conn, supplier, variants):
    pid = _draft(conn, supplier, variants, invoice_no="AB-100")
    cancel_draft_purchase(conn, pid)
    assert find_duplicate_purchase(conn, supplier, "AB-100", "2026-11-01") == []


def test_duplicate_is_only_a_warning_not_a_block(conn, supplier, variants):
    _draft(conn, supplier, variants, invoice_no="AB-100")
    second = _draft(conn, supplier, variants, invoice_no="AB-100")
    assert second is not None