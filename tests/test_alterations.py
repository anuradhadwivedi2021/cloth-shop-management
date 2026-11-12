"""ALT-01 / ALT-02: alteration and tailoring job orders."""

from datetime import date, timedelta

import pytest

from core import alterations as alt
from core.auth import create_user
from core.permissions import PermissionDenied
from tests.test_bill_document import shop  # noqa: F401

TODAY = date.today()
SOON = (TODAY + timedelta(days=3)).isoformat()


def _job(shop, **kw):
    args = dict(item="Blue shirt", work="Shorten sleeves", charge_paise=15000, advance_paise=5000, promised_date=SOON,
                measurements="Sleeve 22in", customer_name="Ravi")
    args.update(kw)
    return alt.create_job(shop.conn, 1, **args)


def test_job_gets_a_number_and_balance(shop):
    j = alt.get_job(shop.conn, 1, _job(shop))
    assert j["job_no"].startswith("ALT/") and j["status"] == "received" and j["balance_paise"] == 10000


def test_checks(shop):
    for bad in (dict(item=" "), dict(work=""), dict(charge_paise=-1), dict(advance_paise=20000),
                dict(promised_date="2020-01-01"), dict(promised_date="nope"), dict(customer_id=999)):
        with pytest.raises(ValueError):
            _job(shop, **bad)


def test_status_flow_and_delivery_takes_balance(shop):
    jid = _job(shop)
    with pytest.raises(ValueError):
        alt.set_status(shop.conn, 1, jid, "delivered")        # cannot skip ahead
    alt.set_status(shop.conn, 1, jid, "in_progress")
    alt.set_status(shop.conn, 1, jid, "ready")
    assert [j["id"] for j in alt.ready_for_pickup(shop.conn, 1)] == [jid]
    done = alt.set_status(shop.conn, 1, jid, "delivered")
    assert done["balance_paid_paise"] == 10000 and done["delivered_at"]
    with pytest.raises(ValueError):
        alt.update_job(shop.conn, 1, jid, work="x")           # closed
    with pytest.raises(ValueError):
        alt.set_status(shop.conn, 1, jid, "cancelled")


def test_update_job(shop):
    jid = _job(shop)
    assert alt.update_job(shop.conn, 1, jid, charge_paise=20000, work="Shorten sleeves") == ["charge_paise"]
    assert alt.update_job(shop.conn, 1, jid, work="Shorten sleeves") == []
    with pytest.raises(ValueError):
        alt.update_job(shop.conn, 1, jid, advance_paise=30000)
    with pytest.raises(ValueError):
        alt.update_job(shop.conn, 1, jid, status="ready")


def test_overdue_list(shop):
    jid = _job(shop)
    assert alt.overdue(shop.conn, 1, today=TODAY) == []
    late = alt.overdue(shop.conn, 1, today=TODAY + timedelta(days=5))
    assert [(j["id"], j["days_late"]) for j in late] == [(jid, 2)]
    alt.set_status(shop.conn, 1, jid, "ready")
    assert alt.overdue(shop.conn, 1, today=TODAY + timedelta(days=5)) == []


def test_permission_and_no_delete(shop):
    stock = create_user(shop.conn, 1, "stk", "Pass#12345", "stock")
    with pytest.raises(PermissionDenied):
        alt.list_jobs(shop.conn, stock)
    jid = _job(shop)
    with pytest.raises(Exception):
        shop.conn.execute("DELETE FROM alteration_jobs WHERE id = ?", (jid,))
    assert shop.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'alteration_created'").fetchone()[0] == 1
