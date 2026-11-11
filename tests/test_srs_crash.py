"""SRS section 10, "Crash": kill the app during bill save, during backup and during restore.

Verify there are no half records and no duplicate or missing bill number.

A separate process (tests/_crash_child.py) is stopped the hard way, os._exit() or SIGKILL: no clean-up, no
commit, no rollback, the same as a power cut. The parent then opens the database again and checks it:

  * the file passes SQLite's integrity check and the audit log's hash chain is unbroken
  * every bill is whole: it has lines, its payments add up to its total, the stock moved by exactly its pieces
  * bill numbers are 1, 2, 3 ... with no gap and no repeat, and the next bill continues the run

For bill save the process is stopped at EVERY one of the SQL statements a bill runs, so no crash point is
left out. Backup and restore are stopped just before and just after each file swap.
"""

import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import backup, shop
from core.audit import verify_chain
from core.products import create_product, generate_variants
from core.sales import create_sale
from core.stock import add_movement, get_stock
from db.connection import open_db, transaction

CHILD = str(Path(__file__).with_name("_crash_child.py"))
OPENING_STOCK = 1_000_000


def _child(*args, timeout=120):
    return subprocess.run([sys.executable, CHILD, *map(str, args)], capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def world(tmp_path):
    folder = tmp_path / "shop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, OPENING_STOCK, "opening stock")
    key = owner.db_key
    conn.close()
    return folder, key, variant


def _check_whole(folder, key, variant, *, expect_sales=None, after_restore=False):
    """Open the database after a crash and prove that nothing is half done. Returns the number of bills."""
    conn = open_db(Path(folder) / "shop.db", key)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verify_chain(conn)[0] is True
        sales = conn.execute("SELECT id, number, invoice_no, total_paise FROM sales ORDER BY number").fetchall()
        assert [r["number"] for r in sales] == list(range(1, len(sales) + 1)), "bill numbers have a gap or a repeat"
        assert len({r["invoice_no"] for r in sales}) == len(sales)
        pieces = 0
        for sale in sales:
            lines = conn.execute("SELECT qty, line_total_paise FROM sale_lines WHERE sale_id = ?", (sale["id"],)).fetchall()
            assert lines, f"{sale['invoice_no']} has no lines"
            paid = conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE document_type = 'sale' "
                                "AND document_id = ?", (sale["id"],)).fetchone()[0]
            assert paid == sale["total_paise"], f"{sale['invoice_no']}: payments do not add up to the total"
            pieces += sum(l["qty"] for l in lines)
        assert get_stock(conn, variant) == OPENING_STOCK - pieces, "stock does not match the bills"
        counter = conn.execute("SELECT COALESCE(MAX(last_number), 0) FROM number_series WHERE series = 'INV'").fetchone()[0]
        if after_restore:
            # a restore goes back in time but never lowers the counter: numbers already printed are not reused
            assert counter >= len(sales), "the bill counter went backwards"
        else:
            assert counter == len(sales), "the bill counter and the saved bills disagree"
        if expect_sales is not None:
            assert len(sales) == expect_sales
        return len(sales)
    finally:
        conn.close()


def _next_bill_continues(folder, key, variant, expected_number):
    conn = open_db(Path(folder) / "shop.db", key)
    try:
        sale_id = create_sale(conn, [{"variant_id": variant, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                              user_id=1, on_date="2026-11-07")
        assert conn.execute("SELECT number FROM sales WHERE id = ?", (sale_id,)).fetchone()[0] == expected_number
    finally:
        conn.close()


# ---------------------------------------------------------------- during bill save

def test_a_bill_save_has_a_known_number_of_steps(world):
    folder, key, _ = world
    steps = int(_child("count", folder, key).stdout.strip())
    assert 10 <= steps <= 200


def test_killed_at_every_step_of_a_bill_save_leaves_no_half_bill(world):
    folder, key, variant = world
    steps = int(_child("count", folder, key).stdout.strip())
    done = _check_whole(folder, key, variant, expect_sales=1)      # the counting run saved one complete bill
    for k in range(1, steps + 1):
        run = _child("sale", folder, key, k)
        assert run.returncode == 137, f"the process should have been killed at step {k}: {run.stderr[-300:]}"
        # killed before COMMIT, so the new bill must not exist at all, and the old state is untouched
        assert _check_whole(folder, key, variant, expect_sales=done) == done, f"half-saved bill after a crash at step {k}"
        _next_bill_continues(folder, key, variant, done + 1)        # no gap, no duplicate number
        done += 1


def test_a_hard_kill_in_the_middle_of_a_stream_of_bills(world):
    folder, key, variant = world
    rng = random.Random(7)
    total = 0
    for _ in range(6):
        proc = subprocess.Popen([sys.executable, CHILD, "loop", str(folder), key], stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(rng.uniform(0.05, 0.6))
        proc.send_signal(signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
        proc.wait(timeout=30)
        total = _check_whole(folder, key, variant)
    assert total >= 1
    _next_bill_continues(folder, key, variant, total + 1)


# ---------------------------------------------------------------- during backup

@pytest.mark.parametrize("when", ["before", "after"])
def test_killed_around_the_moment_a_backup_file_appears(world, tmp_path, when):
    folder, key, variant = world
    dest = tmp_path / "backups"
    assert _child("backup", folder, key, dest, when).returncode == 137
    files = sorted(dest.glob("*.csbak")) if dest.exists() else []
    if when == "before":
        assert files == []                                          # the file appears complete or not at all
    else:
        assert len(files) == 1 and backup.verify_backup_file(files[0])["intact"] is True
    _check_whole(folder, key, variant)                              # the live shop is untouched either way


# ---------------------------------------------------------------- during restore

def _restore_world(world, tmp_path):
    """A shop with 3 bills, a backup of it, and then 2 more bills (so a restore goes BACK in time)."""
    folder, key, variant = world
    conn = open_db(Path(folder) / "shop.db", key)
    for _ in range(3):
        create_sale(conn, [{"variant_id": variant, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                    user_id=1, on_date="2026-11-07")
    conn.close()
    conn, session = shop.open_shop(folder, "owner", "owner-password")
    result = backup.create_backup(conn, session, folder, tmp_path / "bk")
    for _ in range(2):
        create_sale(conn, [{"variant_id": variant, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                    user_id=1, on_date="2026-11-07")
    conn.close()
    return result["path"]


@pytest.mark.parametrize("calls_before_death, dies_before, bills_left", [
    (1, ".csbak", 5),      # while making the safety backup: nothing replaced yet
    (2, "shop.db", 5),     # safety backup done, the database file not yet swapped
    (3, "shop.keys", 3),   # database swapped (3 bills of the backup), key file not yet swapped
])
def test_killed_during_restore_leaves_a_shop_that_still_opens(world, tmp_path, calls_before_death, dies_before,
                                                              bills_left):
    """os.replace is called 3 times: the safety backup, the database file, the key file. Die before each one."""
    folder, key, variant = world
    path = _restore_world(world, tmp_path)
    run = _child("restore", folder, key, path, "owner", "owner-password", calls_before_death)
    assert run.returncode == 137 and f"DIED before replacing" in run.stderr and dies_before in run.stderr, run.stderr[-400:]
    # either the old state (5 bills) or the restored one (3 bills), never anything in between
    assert _check_whole(folder, key, variant, after_restore=True) == bills_left
    conn, session = shop.open_shop(folder, "owner", "owner-password")    # the shop can still be signed in to
    conn.close()


def test_a_finished_restore_never_takes_the_bill_counter_backwards(world, tmp_path):
    folder, key, variant = world
    path = _restore_world(world, tmp_path)
    conn, session = shop.open_shop(folder, "owner", "owner-password")
    result = backup.restore_backup(folder, path, "owner", "owner-password", conn=conn, session=session, confirm=True)
    assert result["bill_count"] == 3
    conn, session = shop.open_shop(folder, "owner", "owner-password")
    try:
        counter = conn.execute("SELECT last_number FROM number_series WHERE series = 'INV'").fetchone()[0]
        assert counter == 5          # the 3 bills of the backup, but the counter keeps the 5 numbers already used
        sale_id = create_sale(conn, [{"variant_id": variant, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                              user_id=session.user_id, on_date="2026-11-07")
        assert conn.execute("SELECT number FROM sales WHERE id = ?", (sale_id,)).fetchone()[0] == 6
    finally:
        conn.close()
