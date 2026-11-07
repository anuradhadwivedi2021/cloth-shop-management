import ast
import glob
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import auth
from core.audit import log_event
from core.permissions import PermissionDenied, set_user_permission
from core.product_edit import change_prices
from core.products import create_product, generate_variants
from core.reports_audit import (
    CATEGORIES, SRS_CATEGORIES, audit_report, category_of, check_audit_chain,
)
from core.stock import add_movement
from core.stock_ops import mark_damaged
from db.connection import open_db, transaction

TODAY = date.today().isoformat()
RANGE = (TODAY, TODAY)


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    ca = auth.create_user(conn, owner, "ca", "ca-password-1", "accountant")

    product = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000, default_cost_paise=50000)
    (variant,) = generate_variants(conn, product, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, 10, "opening stock", user_id=owner)
    change_prices(conn, owner, product, price_paise=110000, cost_paise=60000)   # real: price_changed
    mark_damaged(conn, variant, 2, "torn at hem", user_id=owner)                 # real: stock_damaged

    def event(action, user=None, **kw):
        with transaction(conn):
            log_event(conn, action, user_id=user, **kw)

    event("discount_override", till, entity="sale", entity_id=7,
          after={"approved_by": owner, "discount_percent": 25.0, "limit_percent": 10.0})
    event("sale_reprint", till, entity="sale", entity_id=7, after={"copy": 2})
    event("sale_return_created", till, entity="sale", entity_id=7)
    event("login", till)
    event("login_failed", None, after={"username": "ghost"})
    event("owner_pin_failed", till)
    event("backup_created", owner, after={"file": "backup-1"})
    yield SimpleNamespace(conn=conn, owner=owner, till=till, ca=ca, product=product, variant=variant, event=event)
    conn.close()


def _actions(report):
    return [r["action"] for r in report["rows"]]


# ---------- grouping ----------

def test_actions_map_to_the_srs_groups():
    assert category_of("price_changed") == "prices"
    assert category_of("stock_damaged") == "stock_adjustments"
    assert category_of("stock_returned_ok") == "stock_adjustments"
    assert category_of("stock_count_applied") == "stock_adjustments"
    assert category_of("discount_override") == "discounts"
    assert category_of("sale_reprint") == "bills"
    assert category_of("sale_return_created") == "bills"
    assert category_of("login_failed") == "logins"
    assert category_of("brand_new_unknown_action") == "other"
    assert set(SRS_CATEGORIES) <= set(CATEGORIES)


def test_every_action_the_app_records_has_a_group():
    """A new log_event(...) action added to the code must be given a group, or this test fails."""
    unknown = set()
    for path in glob.glob("core/*.py"):
        tree = ast.parse(open(path, encoding="utf-8-sig").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "log_event":
                arg = node.args[1] if len(node.args) > 1 else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and category_of(arg.value) == "other":
                    unknown.add(arg.value)
    assert unknown == set()


# ---------- permissions and inputs ----------

def test_needs_view_reports(shop):
    with pytest.raises(PermissionDenied):
        audit_report(shop.conn, *RANGE, user_id=shop.till)
    with pytest.raises(PermissionDenied):
        check_audit_chain(shop.conn, user_id=shop.till)
    assert audit_report(shop.conn, *RANGE, user_id=shop.ca)["total"] > 0


def test_bad_inputs_are_rejected(shop):
    c, o = shop.conn, shop.owner
    with pytest.raises(ValueError):
        audit_report(c, TODAY, "2000-01-01", user_id=o)
    with pytest.raises(ValueError):
        audit_report(c, *RANGE, user_id=o, category="nonsense")
    for bad in (0, -1, 5001, 1.5, True, None):
        with pytest.raises(ValueError):
            audit_report(c, *RANGE, user_id=o, limit=bad)


# ---------- the list ----------

def test_newest_first_with_user_names(shop):
    out = audit_report(shop.conn, *RANGE, user_id=shop.owner)
    ids = [r["id"] for r in out["rows"]]
    assert ids == sorted(ids, reverse=True)
    assert out["shown"] == out["total"] == len(out["rows"])
    by_action = {r["action"]: r for r in out["rows"]}
    assert by_action["backup_created"]["user_name"] == "Anuradha"
    assert by_action["login"]["user_name"] == "till"
    assert by_action["login_failed"]["user_name"] == "(system)"     # nobody was logged in
    assert by_action["login_failed"]["label"] == "Login failed"


def test_the_five_srs_groups_can_be_listed(shop):
    c, o = shop.conn, shop.owner
    assert _actions(audit_report(c, *RANGE, user_id=o, category="prices")) == ["price_changed"]
    assert _actions(audit_report(c, *RANGE, user_id=o, category="stock_adjustments")) == ["stock_damaged"]
    assert _actions(audit_report(c, *RANGE, user_id=o, category="discounts")) == ["discount_override"]
    assert _actions(audit_report(c, *RANGE, user_id=o, category="bills")) == ["sale_return_created", "sale_reprint"]
    assert sorted(_actions(audit_report(c, *RANGE, user_id=o, category="logins"))) == [
        "login", "login_failed", "owner_pin_failed"]


def test_unknown_actions_show_up_under_other(shop):
    shop.event("something_added_later", shop.owner)
    out = audit_report(shop.conn, *RANGE, user_id=shop.owner, category="other")
    assert _actions(out) == ["something_added_later"]
    assert out["rows"][0]["category"] == "other"


def test_discount_details_are_readable(shop):
    (row,) = audit_report(shop.conn, *RANGE, user_id=shop.owner, category="discounts")["rows"]
    assert (row["entity"], row["entity_id"]) == ("sale", 7)
    assert row["after"]["discount_percent"] == 25.0 and row["after"]["limit_percent"] == 10.0
    assert "discount_percent: 25.0" in row["detail"]


def test_filters_by_user_action_and_search(shop):
    c, o = shop.conn, shop.owner
    till_rows = audit_report(c, *RANGE, user_id=o, by_user=shop.till)
    assert {r["user_name"] for r in till_rows["rows"]} == {"till"}
    assert _actions(audit_report(c, *RANGE, user_id=o, action="login")) == ["login"]
    assert sorted(_actions(audit_report(c, *RANGE, user_id=o, search="reprint"))) == ["sale_reprint"]
    assert {r["user_name"] for r in audit_report(c, *RANGE, user_id=o, search="till")["rows"]} == {"till"}
    both = audit_report(c, *RANGE, user_id=o, by_user=shop.till, category="logins")
    assert sorted(_actions(both)) == ["login", "owner_pin_failed"]


def test_search_does_not_look_inside_the_details(shop):
    # the cost 60000 appears only inside the details of the price change
    assert audit_report(shop.conn, *RANGE, user_id=shop.owner, search="60000")["rows"] == []


# ---------- cost privacy ----------

def test_costs_are_hidden_from_users_without_view_cost(shop):
    owner_row = audit_report(shop.conn, *RANGE, user_id=shop.owner, category="prices")["rows"][0]
    assert owner_row["after"] == {"default_price_paise": 110000, "default_cost_paise": 60000}
    assert owner_row["before"]["default_cost_paise"] == 50000

    set_user_permission(shop.conn, shop.owner, shop.ca, "view_cost", False)
    ca_row = audit_report(shop.conn, *RANGE, user_id=shop.ca, category="prices")["rows"][0]
    assert ca_row["after"] == {"default_price_paise": 110000}
    assert ca_row["before"] == {"default_price_paise": 105000}
    assert "cost" not in ca_row["detail"]


# ---------- counts, period and limit ----------

def test_counts_cover_every_group_and_user(shop):
    out = audit_report(shop.conn, *RANGE, user_id=shop.owner)
    assert set(out["by_category"]) == set(CATEGORIES)
    assert sum(out["by_category"].values()) == out["total"]
    assert out["by_category"]["prices"] == 1 and out["by_category"]["bills"] == 2
    assert out["by_category"]["users"] >= 2                       # owner and the two staff accounts were created
    assert sum(u["count"] for u in out["by_user"]) == out["total"]
    assert out["by_user"][0]["count"] >= out["by_user"][-1]["count"]
    assert any(u["user_name"] == "(system)" for u in out["by_user"])
    # a category filter narrows the rows but not the group totals
    only_prices = audit_report(shop.conn, *RANGE, user_id=shop.owner, category="prices")
    assert only_prices["total"] == 1
    assert only_prices["by_category"] == out["by_category"]


def test_only_entries_inside_the_period_are_listed(shop):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    out = audit_report(shop.conn, yesterday, yesterday, user_id=shop.owner)
    assert out["rows"] == [] and out["total"] == 0
    assert sum(out["by_category"].values()) == 0
    wide = audit_report(shop.conn, yesterday, TODAY, user_id=shop.owner)
    assert wide["total"] == audit_report(shop.conn, *RANGE, user_id=shop.owner)["total"]


def test_limit_keeps_the_newest_entries(shop):
    full = audit_report(shop.conn, *RANGE, user_id=shop.owner)
    out = audit_report(shop.conn, *RANGE, user_id=shop.owner, limit=3)
    assert out["shown"] == 3 and out["total"] == full["total"] > 3
    assert [r["id"] for r in out["rows"]] == [r["id"] for r in full["rows"][:3]]


# ---------- real actions land in the right group ----------

def test_real_price_and_stock_changes_are_reported(shop):
    out = audit_report(shop.conn, *RANGE, user_id=shop.owner, category="stock_adjustments")
    assert out["rows"][0]["after"]["reason"] == "torn at hem"
    assert out["rows"][0]["after"]["qty"] == 2
    prices = audit_report(shop.conn, *RANGE, user_id=shop.owner, category="prices")
    assert prices["rows"][0]["entity"] == "product"
    assert prices["rows"][0]["entity_id"] == shop.product


# ---------- tamper check ----------

def test_chain_check_passes_then_catches_an_edit(shop):
    ok = check_audit_chain(shop.conn, user_id=shop.owner)
    assert ok["ok"] is True and ok["first_bad_id"] is None and ok["entries"] > 5

    victim = shop.conn.execute("SELECT id FROM audit_log WHERE action = 'sale_reprint'").fetchone()[0]
    shop.conn.execute("DROP TRIGGER audit_log_no_update")        # what someone editing the database file could do
    shop.conn.execute("UPDATE audit_log SET action = 'login' WHERE id = ?", (victim,))
    broken = check_audit_chain(shop.conn, user_id=shop.owner)
    assert broken["ok"] is False and broken["first_bad_id"] == victim
