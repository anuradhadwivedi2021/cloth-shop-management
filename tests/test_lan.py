"""Second counter on the shop network (LAN): the safety rules and two counters billing on one shop."""

import threading

import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from core import shop
from core.auth import AuthError
from core.sales import create_sale
from lan.client import RemoteConnection, connect_counter
from lan.protocol import LanError, check_private_address, check_statement, derive_key, new_pairing_code, seal, unseal
from lan.server import CounterServer
from tests.test_bill_document import DAY, _cart, shop as shop_fixture  # noqa: F401

CODE = "K7M2-QX9P-4T"


@pytest.fixture
def main_counter(shop_fixture):
    """The shop fixture is the main PC; its database file is shared through a CounterServer on 127.0.0.1."""
    db_path = shop_fixture.conn.execute("PRAGMA database_list").fetchone()["file"]
    server = CounterServer(db_path, "test-password", CODE, "127.0.0.1")
    server.start()
    shop_fixture.server = server
    yield shop_fixture
    server.stop()


def test_only_shop_network_addresses_are_allowed():
    for ok in ("192.168.1.20", "10.0.0.5", "172.16.4.9", "127.0.0.1"):
        assert check_private_address(ok) == ok
    for bad in ("8.8.8.8", "142.250.1.1", "0.0.0.0", "shop.example.com", ""):
        with pytest.raises(LanError):
            check_private_address(bad)
    with pytest.raises(LanError):
        CounterServer("x.db", "k", CODE, "8.8.8.8")
    with pytest.raises(LanError):
        RemoteConnection("8.8.8.8", 8765, CODE)


def test_messages_are_encrypted_and_tamper_proof():
    key = derive_key(CODE)
    blob = seal(key, {"op": "ping", "secret": "CustomerName"})
    assert b"CustomerName" not in blob and unseal(key, blob)["op"] == "ping"
    with pytest.raises(LanError):
        unseal(derive_key("AAAA-BBBB-CC"), blob)                 # wrong code
    with pytest.raises(LanError):
        unseal(key, blob[:-1] + bytes([blob[-1] ^ 1]))           # changed in transit
    assert len(new_pairing_code().replace("-", "")) == 10


def test_only_data_statements_are_accepted():
    for ok in ("SELECT 1", "  insert into t values (1)", "BEGIN IMMEDIATE", "COMMIT", "WITH a AS (SELECT 1) SELECT * FROM a"):
        check_statement(ok)
    for bad in ("PRAGMA key = 'x'", "ATTACH DATABASE 'x' AS y", "VACUUM INTO 'x'", "DROP TABLE sales",
                "SELECT load_extension('x')", "CREATE TABLE t(a)"):
        with pytest.raises(LanError):
            check_statement(bad)


def test_wrong_code_is_refused(main_counter):
    with pytest.raises(LanError):
        RemoteConnection("127.0.0.1", main_counter.server.port, "AAAA-BBBB-CC")


def test_second_counter_reads_and_writes_the_same_shop(main_counter):
    s = main_counter
    conn = RemoteConnection("127.0.0.1", s.server.port, CODE)
    row = conn.execute("SELECT id, name FROM products ORDER BY id LIMIT 1").fetchone()
    assert row["name"] == row[1] and row.keys() == ["id", "name"]
    before = s.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    sale_id = create_sale(conn, _cart(s), [{"mode": "cash", "amount_paise": 623000}], user_id=s.user, on_date=DAY)
    # the main counter sees the bill the second counter made, and the next bill number follows on
    assert s.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == before + 1
    main_id = create_sale(s.conn, _cart(s), [{"mode": "cash", "amount_paise": 623000}], user_id=s.user, on_date=DAY)
    numbers = [r[0] for r in s.conn.execute("SELECT invoice_no FROM sales WHERE id IN (?, ?) ORDER BY id", (sale_id, main_id))]
    assert len(set(numbers)) == 2
    conn.close()


def test_errors_and_rollback_behave_like_a_normal_connection(main_counter):
    conn = RemoteConnection("127.0.0.1", main_counter.server.port, CODE)
    conn.execute("BEGIN IMMEDIATE")
    assert conn.in_transaction
    with pytest.raises(sqlcipher.IntegrityError):
        conn.execute("INSERT INTO users(id, username) VALUES (1, 'dup')")
    conn.execute("ROLLBACK")
    assert not conn.in_transaction
    with pytest.raises(LanError):
        conn.execute("DROP TABLE sales")
    conn.close()


def test_blobs_travel(main_counter):
    conn = RemoteConnection("127.0.0.1", main_counter.server.port, CODE)
    assert conn.execute("SELECT x'00ff10'").fetchone()[0] == b"\x00\xff\x10"
    conn.close()


def test_two_counters_billing_at_once_get_different_bill_numbers(main_counter):
    s = main_counter
    results, errors = [], []

    def bill():
        try:
            conn = RemoteConnection("127.0.0.1", s.server.port, CODE)
            for _ in range(3):
                results.append(create_sale(conn, [{"variant_id": s.m_blue, "qty": 1}],
                                           [{"mode": "cash", "amount_paise": 110250}], user_id=s.user, on_date=DAY))
            conn.close()
        except Exception as exc:                      # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=bill) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(set(results)) == 6
    numbers = [r[0] for r in s.conn.execute("SELECT invoice_no FROM sales WHERE id IN (%s)" % ",".join("?" * 6), results)]
    assert len(set(numbers)) == 6


def test_sign_in_from_the_second_counter(tmp_path):
    folder = tmp_path / "shop"
    conn, session, _ = shop.setup_new_shop(folder, "owner", "Owner#12345", pin="1234")
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    server = CounterServer(db_path, session.db_key, CODE, "127.0.0.1")
    server.start()
    try:
        remote, remote_session = connect_counter("127.0.0.1", server.port, CODE, "owner", "Owner#12345")
        assert remote_session.username == "owner" and remote_session.db_key is None
        remote.close()
        with pytest.raises(AuthError):
            connect_counter("127.0.0.1", server.port, CODE, "owner", "wrong-password")
    finally:
        server.stop()
        conn.close()


def test_a_counter_that_stops_mid_save_does_not_block_the_other(main_counter, monkeypatch):
    import lan.server as srv
    monkeypatch.setattr(srv, "IDLE_IN_TRANSACTION_SECONDS", 0.2)
    s = main_counter
    stuck = RemoteConnection("127.0.0.1", s.server.port, CODE)
    stuck.execute("BEGIN IMMEDIATE")
    import time
    time.sleep(6)                                       # the reaper wakes every 5 s
    other = RemoteConnection("127.0.0.1", s.server.port, CODE)
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")
    other.close()
