"""STK-07/08: stock ageing report, and selecting aged products for a markdown in Bulk Edit."""

import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import auth, bulk_edit as be, product_edit as pe, shop, stock_ageing as sa  # noqa: E402

TODAY = date(2026, 11, 10)


def _day(n):
    return (TODAY - timedelta(days=n)).isoformat() + " 10:00:00"


def _move(conn, vid, qty, days_ago, doc="purchase"):
    conn.execute("INSERT INTO stock_movements(variant_id, qty_change, bucket, reason, doc_type, balance_after, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (vid, qty, "sellable", "t", doc, qty, _day(days_ago)))
    conn.commit()


@pytest.fixture
def world(tmp_path):
    conn, owner, _ = shop.setup_new_shop(tmp_path / "shop", "owner", "owner-password", pin="1234")
    ids = {}
    for name, season in (("Old coat", "Winter"), ("New tee", "Summer"), ("Mixed kurta", "Summer")):
        r = pe.add_product(conn, owner.user_id, name, sizes=["M", "L"], colours=["Red"], season=season,
                           default_price_paise=100000)
        ids[name] = (r["product_id"], r["variant_ids"])
    _move(conn, ids["Old coat"][1][0], 5, 200)
    _move(conn, ids["Old coat"][1][1], 3, 120)
    _move(conn, ids["New tee"][1][0], 4, 10)
    _move(conn, ids["Mixed kurta"][1][0], 2, 150)
    _move(conn, ids["Mixed kurta"][1][1], 2, 20)
    yield conn, owner, ids
    conn.close()


def test_bands_and_age_come_from_the_last_purchase(world):
    conn, owner, ids = world
    _move(conn, ids["Old coat"][1][0], 1, 5, doc="sale_return")        # a return does not make stock new
    _move(conn, ids["Old coat"][1][0], -2, 3, doc="sale")              # a sale does not either
    res = sa.stock_ageing(conn, user_id=owner.user_id, on_date=TODAY.isoformat())
    by = {(r["product"], r["size"]): r for r in res["rows"]}
    assert by[("Old coat", "M")]["age_days"] == 200 and by[("Old coat", "M")]["band"] == "Over 180 days"
    assert by[("Old coat", "L")]["band"] == "91-180 days" and by[("New tee", "M")]["band"] == "0-30 days"
    assert res["rows"][0]["age_days"] == 200                           # oldest first
    assert {b["band"]: b["pieces"] for b in res["bands"]}["0-30 days"] == 6 and res["units_in_stock"] == sum(
        r["sellable_qty"] for r in res["rows"])


def test_a_new_purchase_resets_the_age_and_filters_work(world):
    conn, owner, ids = world
    _move(conn, ids["Old coat"][1][0], 1, 2)
    res = sa.stock_ageing(conn, user_id=owner.user_id, on_date=TODAY.isoformat(), season="winter", min_age_days=91)
    assert [(r["size"], r["age_days"]) for r in res["rows"]] == [("L", 120)]
    with pytest.raises(ValueError):
        sa.stock_ageing(conn, user_id=owner.user_id, min_age_days=-1)


def test_aged_products_need_ALL_their_stock_to_be_old(world):
    conn, owner, ids = world
    assert sa.aged_product_ids(conn, owner.user_id, 90, on_date=TODAY.isoformat()) == [ids["Old coat"][0]]
    assert ids["Mixed kurta"][0] not in sa.aged_product_ids(conn, owner.user_id, 90, on_date=TODAY.isoformat())


def test_out_of_stock_variants_are_not_listed(world):
    conn, owner, ids = world
    _move(conn, ids["Old coat"][1][0], -5, 1, doc="sale")
    res = sa.stock_ageing(conn, user_id=owner.user_id, on_date=TODAY.isoformat())
    assert ("Old coat", "M") not in {(r["product"], r["size"]) for r in res["rows"]}


def test_permissions_and_cost(world):
    conn, owner, ids = world
    res = sa.stock_ageing(conn, user_id=owner.user_id, on_date=TODAY.isoformat())
    assert "stock_value_paise" in res["bands"][0]
    low = auth.create_user(conn, owner.user_id, "cash", "cash-password-1", role="cashier")
    with pytest.raises(ValueError):
        sa.stock_ageing(conn, user_id=low, on_date=TODAY.isoformat())


def test_markdown_of_aged_stock_through_bulk_edit(world):
    conn, owner, ids = world
    old = be.find_products(conn, owner.user_id, "", older_than_days=90)    # age is counted to the real today
    assert "Old coat" in [r["name"] for r in old]
    pid = ids["Old coat"][0]
    rows = pe.bulk_edit_products(conn, owner.user_id, [pid], {"price_change_percent": -30}, apply=True)
    assert rows[0]["new"] == 70000
    with pytest.raises(ValueError):
        be.parse_days("abc")
    assert be.parse_days("") is None and be.parse_days("90") == 90
