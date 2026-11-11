"""SRS section 9 (Performance targets) and section 10 (Performance): measure the numbers the SRS promises.

  Application start to login screen            5 s or less
  Barcode scan to line added                   0.3 s or less
  Product search results while typing          0.5 s or less
  Save and print a 10-line bill                2 s or less (excluding printer speed)
  Daily or monthly report                      3 s or less; one-year report 10 s or less
  Full encrypted backup of 1 GB                2 minutes or less

Normal runs use a SMALL shop (2,000 variants, 2,000 bills) so the whole test suite stays quick; it proves the
code paths work and catches a big slowdown. The real acceptance run uses the SRS size:

    CLOTHSHOP_PERF=1 python -m pytest tests/test_srs_performance.py -s

which loads 50,000 variants and 100,000 bills over 5 years (the shop is built with the real bill-saving code,
so the data is genuine), then checks every target. Other knobs: CLOTHSHOP_PERF_VARIANTS, CLOTHSHOP_PERF_BILLS,
and CLOTHSHOP_PERF_SLACK (multiply every limit, e.g. 2 for a slower PC), CLOTHSHOP_PERF_REPORT (write the
measured numbers to this JSON file). The limits are for the SRS "minimum PC"; run the full test on that PC
before saying the targets are met.
"""

import json
import os
import random
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import backup, shop  # noqa: E402
from core import bill_preview as bp  # noqa: E402
from core import reports_sales as rs  # noqa: E402
from core import reports_stock as rst  # noqa: E402
from core.products import create_product, generate_variants, search_products  # noqa: E402
from core.sales import create_sale, save_sale_in_transaction  # noqa: E402
from core.stock import add_movement  # noqa: E402
from db.connection import open_db, transaction  # noqa: E402

FULL = os.environ.get("CLOTHSHOP_PERF") == "1"
N_VARIANTS = int(os.environ.get("CLOTHSHOP_PERF_VARIANTS", 50_000 if FULL else 2_000))
N_BILLS = int(os.environ.get("CLOTHSHOP_PERF_BILLS", 100_000 if FULL else 2_000))
SLACK = float(os.environ.get("CLOTHSHOP_PERF_SLACK", "1"))
YEARS = 5
SIZES = ["S", "M", "L", "XL", "XXL"]
COLOURS = ["Blue", "Black"]
NAMES = ["Cotton Shirt", "Denim Jeans", "Silk Saree", "Kurta", "Blazer", "Lehenga", "T-Shirt", "Trousers", "Dupatta",
         "Salwar Suit", "Jacket", "Skirt", "Sherwani", "Shorts", "Kids Frock"]
BRANDS = ["Raymond", "Peter England", "Fab India", "Biba", "Levis", "Allen Solly", "Local"]
CATEGORIES = ["Men", "Women", "Kids", "Ethnic"]
PRICES = [49900, 105000, 199900]          # whole rupees, GST included, so a bill total equals the sum of its lines
RESULTS = {}


def _build_shop(folder, n_variants, n_bills, rng):
    """A shop of the given size, built with the real code. Returns (key, variant_ids, started_at, built_at)."""
    conn, owner, _ = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    key = owner.db_key
    with transaction(conn):                    # the default GST rules start in 2025; the old bills need them from 2021
        conn.execute("UPDATE tax_rules SET valid_from = '2000-01-01'")
    per_product = len(SIZES) * len(COLOURS)
    variants = []
    for i in range(max(1, n_variants // per_product)):
        name = f"{rng.choice(NAMES)} {i + 1}"
        pid = create_product(conn, name, category=rng.choice(CATEGORIES), brand=rng.choice(BRANDS), hsn="6205",
                             default_price_paise=rng.choice(PRICES), default_cost_paise=30000, mrp_paise=210000)
        variants += generate_variants(conn, pid, SIZES, COLOURS)
    with transaction(conn):
        for v in variants:
            add_movement(conn, v, 60, "opening stock")
    start = date.today() - timedelta(days=365 * YEARS - 1)
    step = 1000
    for first in range(0, n_bills, step):
        with transaction(conn):
            for i in range(first, min(first + step, n_bills)):
                day = start + timedelta(days=i * (365 * YEARS - 1) // max(n_bills, 1))
                picks = rng.sample(variants, rng.choice([1, 1, 2, 3]))
                price = {v: conn.execute("SELECT COALESCE(v.price_override_paise, p.default_price_paise) FROM variants v "
                                         "JOIN products p ON p.id = v.product_id WHERE v.id = ?", (v,)).fetchone()[0]
                         for v in picks}
                save_sale_in_transaction(
                    conn, [{"variant_id": v, "qty": 1} for v in picks],
                    [{"mode": "cash", "amount_paise": sum(price.values())}], user_id=1, on_date=day.isoformat())
    _backdate(conn, start, n_bills)
    conn.close()
    return key, variants, start, date.today()


def _backdate(conn, start, n_bills):
    """Bills are stamped with today's time when saved, and the database refuses to change that (as it should).
    For this test only, the stamps are moved to the spread of days the bills were meant for, so the reports
    see five years of trade. The protecting triggers are put back exactly as they were."""
    names = ("sales_no_edit", "payments_no_update")
    sql = {n: conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (n,)).fetchone()[0]
           for n in names}
    with transaction(conn):
        for n in names:
            conn.execute(f"DROP TRIGGER {n}")
        days = {}
        for i, (sale_id,) in enumerate(conn.execute("SELECT id FROM sales ORDER BY id").fetchall()):
            days[sale_id] = (start + timedelta(days=i * (365 * YEARS - 1) // max(n_bills, 1))).isoformat() + " 12:00:00"
        conn.executemany("UPDATE sales SET created_at = ? WHERE id = ?", [(d, i) for i, d in days.items()])
        conn.executemany("UPDATE payments SET created_at = ? WHERE document_type = 'sale' AND document_id = ?",
                         [(d, i) for i, d in days.items()])
        for n in names:
            conn.execute(sql[n])


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    folder = tmp_path_factory.mktemp("perf") / "shop"
    t0 = time.perf_counter()
    key, variants, first_day, last_day = _build_shop(folder, N_VARIANTS, N_BILLS, random.Random(2026))
    build_seconds = time.perf_counter() - t0
    conn = open_db(folder / "shop.db", key)
    size_mb = (folder / "shop.db").stat().st_size / 1e6
    RESULTS["setup"] = {"variants": len(variants), "bills": N_BILLS, "db_mb": round(size_mb, 1),
                        "build_seconds": round(build_seconds, 1), "full_scale": FULL}
    print(f"\n[perf] built {len(variants):,} variants and {N_BILLS:,} bills in {build_seconds:,.0f}s; database {size_mb:,.0f} MB")
    yield {"folder": folder, "key": key, "conn": conn, "variants": variants, "first": first_day, "last": last_day}
    conn.close()
    path = os.environ.get("CLOTHSHOP_PERF_REPORT")
    if path:
        Path(path).write_text(json.dumps(RESULTS, indent=1), encoding="utf-8")


def _time(fn, repeat=1):
    """Run fn `repeat` times; return the list of seconds."""
    out = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


def _check(name, seconds, limit):
    worst = max(seconds)
    RESULTS[name] = {"worst_s": round(worst, 3), "median_s": round(sorted(seconds)[len(seconds) // 2], 3),
                     "limit_s": limit * SLACK, "runs": len(seconds), "ok": worst <= limit * SLACK}
    print(f"[perf] {name}: worst {worst:.3f}s, median {RESULTS[name]['median_s']:.3f}s (limit {limit * SLACK:g}s)")
    assert worst <= limit * SLACK, f"{name}: {worst:.3f}s is over the {limit * SLACK:g}s target"


def test_the_shop_is_the_size_the_test_says(big):
    conn = big["conn"]
    assert conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0] >= N_VARIANTS * 0.99
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == N_BILLS
    assert conn.execute("SELECT MIN(created_at) FROM sales").fetchone()[0][:4] < str(big["last"].year - YEARS + 2)
    assert rs.series_gap_check(conn, user_id=1) == []                     # 100,000 bills, no gap in the numbers


def test_barcode_scan_to_line_added(big):
    conn, rng = big["conn"], random.Random(1)
    codes = [conn.execute("SELECT barcode FROM variants WHERE id = ?", (v,)).fetchone()[0]
             for v in rng.sample(big["variants"], 60)]

    def scan(code):
        info = bp.find_by_code(conn, code)                               # the scan finds the item ...
        assert info is not None
        bp.preview_bill(conn, [{"variant_id": info["variant_id"], "qty": 1}] * 1)   # ... and the bill is worked out again

    _check("barcode_scan_to_line", [_time(lambda c=c: scan(c))[0] for c in codes], 0.3)


def test_a_sku_scan_is_as_fast(big):
    conn, rng = big["conn"], random.Random(2)
    skus = [conn.execute("SELECT sku FROM variants WHERE id = ?", (v,)).fetchone()[0] for v in rng.sample(big["variants"], 40)]
    _check("sku_scan", [_time(lambda s=s: bp.find_by_code(conn, s))[0] for s in skus], 0.3)


def test_product_search_while_typing(big):
    conn = big["conn"]
    typed = []
    for word in ("Cotton Shirt", "Denim Jeans 12", "Kurta Blue", "Silk Saree", "Raymond", "XL Black", "6205", "Kids"):
        typed += [word[:n] for n in range(1, len(word) + 1)]               # every keystroke of the word
    assert search_products(conn, "Cotton", limit=20), "the search finds nothing, so its speed means nothing"
    _check("product_search_per_keystroke", [_time(lambda t=t: search_products(conn, t, limit=20))[0] for t in typed], 0.5)


def test_save_and_print_a_ten_line_bill(big, tmp_path):
    from PySide6.QtWidgets import QApplication

    from ui import print_service

    QApplication.instance() or QApplication([])
    conn, rng = big["conn"], random.Random(3)
    session = _owner_session(big)
    seconds = []
    for _ in range(12):
        picks = rng.sample(big["variants"], 10)
        total = sum(conn.execute("SELECT COALESCE(v.price_override_paise, p.default_price_paise) FROM variants v "
                                 "JOIN products p ON p.id = v.product_id WHERE v.id = ?", (v,)).fetchone()[0] for v in picks)
        t0 = time.perf_counter()
        sale_id = create_sale(conn, [{"variant_id": v, "qty": 1} for v in picks],
                              [{"mode": "cash", "amount_paise": total}], user_id=1, on_date=date.today().isoformat())
        result = print_service.save_bill_pdf(conn, session, sale_id, str(tmp_path / f"bill{sale_id}.pdf"))
        seconds.append(time.perf_counter() - t0)
        assert result.ok, result.message
    _check("save_and_print_10_line_bill", seconds, 2.0)


def test_daily_and_monthly_reports(big):
    conn, last = big["conn"], big["last"]
    month_start = last.replace(day=1)
    day, month = last - timedelta(days=30), (month_start - timedelta(days=60)).replace(day=1)
    month_end = (month + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    runs = []
    assert rs.sales_summary(conn, month.isoformat(), month_end.isoformat(), user_id=1)["bill_count"] > 0, "empty month"
    assert rs.sales_summary(conn, (last - timedelta(days=364)).isoformat(), last.isoformat(), user_id=1)["bill_count"] > 0
    for name, call in (
        ("daily_sales_summary", lambda: rs.sales_summary(conn, day.isoformat(), day.isoformat(), user_id=1)),
        ("daily_day_book", lambda: rs.day_book(conn, day.isoformat(), day.isoformat(), user_id=1)),
        ("monthly_sales_summary", lambda: rs.sales_summary(conn, month.isoformat(), month_end.isoformat(), user_id=1)),
        ("monthly_sales_by_day", lambda: rs.sales_by(conn, month.isoformat(), month_end.isoformat(), "day", user_id=1)),
        ("monthly_by_payment_mode", lambda: rs.payment_mode_summary(conn, month.isoformat(), month_end.isoformat(), user_id=1)),
        ("monthly_gst_register", lambda: rs.gst_sales_register(conn, month.isoformat(), month_end.isoformat(), user_id=1)),
        ("monthly_day_book", lambda: rs.day_book(conn, month.isoformat(), month_end.isoformat(), user_id=1)),
    ):
        seconds = _time(call, 3)
        _check(name, seconds, 3.0)
        runs.append(name)
    assert len(runs) == 7


def test_stock_reports_on_every_variant(big):
    conn = big["conn"]
    _check("stock_overview_all_variants", _time(lambda: rst.stock_overview(conn, user_id=1), 2), 3.0)
    _check("stock_valuation_all_variants", _time(lambda: rst.stock_valuation(conn, user_id=1), 2), 3.0)
    _check("low_stock_list", _time(lambda: rst.low_stock(conn, user_id=1), 2), 3.0)


def test_one_year_reports(big):
    conn, last = big["conn"], big["last"]
    year_from, year_to = (last - timedelta(days=364)).isoformat(), last.isoformat()
    for name, call in (
        ("year_sales_summary", lambda: rs.sales_summary(conn, year_from, year_to, user_id=1)),
        ("year_sales_by_product", lambda: rs.sales_by(conn, year_from, year_to, "product", user_id=1)),
        ("year_sales_by_day", lambda: rs.sales_by(conn, year_from, year_to, "day", user_id=1)),
        ("year_gst_register", lambda: rs.gst_sales_register(conn, year_from, year_to, user_id=1)),
        ("year_day_book", lambda: rs.day_book(conn, year_from, year_to, user_id=1)),
    ):
        _check(name, _time(call, 2), 10.0)


def test_application_start_to_login_screen(big):
    from PySide6.QtWidgets import QApplication

    from ui.controller import AppController

    QApplication.instance() or QApplication([])
    seconds = _time(lambda: AppController(big["folder"]), 3)
    _check("start_to_login_screen", seconds, 5.0)


def test_encrypted_backup_speed(big, tmp_path):
    """1 GB in 2 minutes means at least 8.5 MB/s. Measured on this shop, then worked out for 1 GB."""
    conn = big["conn"]
    session = _owner_session(big)
    size_mb = (big["folder"] / "shop.db").stat().st_size / 1e6
    t0 = time.perf_counter()
    result = backup.create_backup(conn, session, big["folder"], tmp_path / "bk")
    seconds = time.perf_counter() - t0
    per_gb = seconds * 1000 / size_mb
    RESULTS["backup_1gb_estimate"] = {"db_mb": round(size_mb, 1), "seconds": round(seconds, 2),
                                      "estimated_seconds_per_gb": round(per_gb, 1), "limit_s": 120 * SLACK,
                                      "ok": per_gb <= 120 * SLACK}
    print(f"[perf] backup: {size_mb:,.0f} MB in {seconds:.1f}s, about {per_gb:.0f}s for 1 GB (limit {120 * SLACK:g}s)")
    assert backup.verify_backup_file(result["path"])["intact"] is True
    if size_mb >= 50:        # a tiny database is mostly fixed start-up cost, so it says nothing about 1 GB
        assert per_gb <= 120 * SLACK, f"a 1 GB backup would take about {per_gb:.0f}s, over the 120s target"


def _owner_session(big):
    from types import SimpleNamespace

    from core import keystore  # noqa: F401  (the session only needs the key and the user)

    owner = big["conn"].execute("SELECT id, username, role FROM users WHERE id = 1").fetchone()
    from core.session import Session

    return Session({"id": owner["id"], "username": owner["username"], "role": owner["role"]}, db_key=big["key"])