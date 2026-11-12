"""BIL-14: salesperson on bills and the commission report."""

from datetime import date

import pytest

from core import commission
from core.auth import create_user
from core.permissions import PermissionDenied
from core.returns import process_return
from core.sales import create_sale
from core.settings import set_setting
from tests.test_bill_document import DAY, _cart, shop  # noqa: F401

TODAY = date.today().isoformat()


@pytest.fixture
def two(shop):
    shop.asha = create_user(shop.conn, 1, "asha", "Pass#12345", "cashier", full_name="Asha")
    return shop


def _sell_by(s, person, total=623000):
    return create_sale(s.conn, _cart(s), [{"mode": "cash", "amount_paise": total}], user_id=1, on_date=DAY,
                       salesperson_id=person)


def _row(report, person_id):
    return next(r for r in report["rows"] if r["person_id"] == person_id)


def test_parse_percent():
    assert commission.parse_percent("1.5") == 150
    assert commission.parse_percent("2%") == 200
    for bad in ("abc", "51", "-1", "1.234"):
        with pytest.raises(ValueError):
            commission.parse_percent(bad)


def test_rates_default_and_own(two):
    set_setting(two.conn, "commission_default_bp", "100")
    commission.set_rate(two.conn, 1, two.asha, "2.5")
    rates = {p["id"]: p for p in commission.list_salespeople(two.conn, 1)}
    assert rates[two.asha]["rate_bp"] == 250 and rates[1]["rate_bp"] == 100
    commission.set_rate(two.conn, 1, two.asha, None)
    assert {p["id"]: p for p in commission.list_salespeople(two.conn, 1)}[two.asha]["rate_bp"] == 100


def test_only_settings_managers_change_rates(two):
    with pytest.raises(PermissionDenied):
        commission.set_rate(two.conn, two.asha, two.asha, "5")


def test_report_commission_and_return_clawback(two):
    set_setting(two.conn, "commission_enabled", "1")
    set_setting(two.conn, "commission_default_bp", "200")
    sid = _sell_by(two, two.asha)
    rep = commission.commission_report(two.conn, TODAY, TODAY, user_id=1)
    row = _row(rep, two.asha)
    assert row["bills"] == 1 and row["net_sales_paise"] == row["sales_taxable_paise"] > 0
    assert row["commission_paise"] == round(row["net_sales_paise"] * 0.02)
    line = two.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ? AND variant_id = ?", (sid, two.m_blue)).fetchone()[0]
    process_return(two.conn, sid, [{"sale_line_id": line, "qty": 1, "reason": "wrong_size"}], user_id=1, on_date=DAY)
    after = _row(commission.commission_report(two.conn, TODAY, TODAY, user_id=1), two.asha)
    assert after["returns_taxable_paise"] > 0
    assert after["commission_paise"] < row["commission_paise"]


def test_default_credits_the_biller_and_unknown_person_is_refused(two):
    sid = create_sale(two.conn, _cart(two), [{"mode": "cash", "amount_paise": 623000}], user_id=1, on_date=DAY)
    assert two.conn.execute("SELECT salesperson_id FROM sales WHERE id = ?", (sid,)).fetchone()[0] == 1
    with pytest.raises(ValueError, match="salesperson"):
        _sell_by(two, 9999)


def test_commission_rate_change_is_audited(two):
    commission.set_rate(two.conn, 1, two.asha, "3")
    assert two.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'commission_rate_changed'").fetchone()[0] == 1
