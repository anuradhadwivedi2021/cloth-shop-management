import pytest

from core.audit import log_event, verify_chain
from core.settings import get_bool, get_setting, set_setting
from db.connection import open_db, transaction


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "shop.db", "test-password")
    yield c
    c.close()


def _log(conn, n=3):
    with transaction(conn):
        for i in range(n):
            log_event(conn, "test_event", entity="thing", entity_id=i, after={"n": i})


# ---------- audit chain ----------

def test_log_event_must_be_inside_a_transaction(conn):
    with pytest.raises(RuntimeError):
        log_event(conn, "oops")


def test_action_is_required(conn):
    with pytest.raises(ValueError):
        with transaction(conn):
            log_event(conn, "  ")


def test_empty_log_is_valid(conn):
    assert verify_chain(conn) == (True, None)


def test_entries_are_chained_and_verify(conn):
    _log(conn, 4)
    rows = conn.execute("SELECT id, prev_hash, hash FROM audit_log ORDER BY id").fetchall()
    assert rows[0]["prev_hash"] is None
    for earlier, later in zip(rows, rows[1:]):
        assert later["prev_hash"] == earlier["hash"]
    assert verify_chain(conn) == (True, None)


def test_rolled_back_event_leaves_no_entry(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            log_event(conn, "will_not_happen")
            raise RuntimeError("crash")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_editing_an_old_entry_is_detected(conn):
    _log(conn, 3)
    conn.execute("DROP TRIGGER audit_log_no_update")  # what someone editing the file directly could do
    conn.execute("UPDATE audit_log SET after_json = '{\"n\": 99}' WHERE id = 2")
    assert verify_chain(conn) == (False, 2)


def test_removing_an_entry_is_detected(conn):
    _log(conn, 3)
    conn.execute("DROP TRIGGER audit_log_no_delete")
    conn.execute("DELETE FROM audit_log WHERE id = 2")
    assert verify_chain(conn) == (False, 3)


def test_application_cannot_edit_or_delete_audit_rows(conn):
    _log(conn, 1)
    with pytest.raises(Exception):
        conn.execute("UPDATE audit_log SET action = 'x'")
    with pytest.raises(Exception):
        conn.execute("DELETE FROM audit_log")


# ---------- settings ----------

def test_setting_defaults_and_updates(conn):
    assert get_setting(conn, "credit_sales_enabled") is None
    assert get_bool(conn, "credit_sales_enabled") is False
    set_setting(conn, "credit_sales_enabled", 1)
    assert get_bool(conn, "credit_sales_enabled") is True
    set_setting(conn, "credit_sales_enabled", 0)
    assert get_bool(conn, "credit_sales_enabled") is False


def test_setting_name_is_required(conn):
    with pytest.raises(ValueError):
        set_setting(conn, " ", "x")
