"""PAY-02: the owner chooses which payment methods the shop accepts (cash is always accepted)."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import shop, shop_settings as ss  # noqa: E402
from core.customer_credit import receive_payment  # noqa: E402
from core.permissions import PermissionDenied  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.returns import process_return  # noqa: E402
from core.sales import ACCEPTED_PAYMENT_MODES, create_sale, enabled_payment_modes, get_sale  # noqa: E402
from core.stock import add_movement, get_stock  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import transaction  # noqa: E402

_APP = QApplication.instance() or QApplication([])
PRICE = 105000


@pytest.fixture
def s(tmp_path):
    folder = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=PRICE)
    (variant,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, 50, "opening stock")
    users = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM users")}
    yield SimpleNamespace(folder=folder, conn=conn, owner=owner, uid=owner.user_id, users=users, variant=variant)
    conn.close()


def sell(s, mode="cash"):
    return create_sale(s.conn, [{"variant_id": s.variant, "qty": 1}], [{"mode": mode, "amount_paise": PRICE}], user_id=s.uid)


def switch(s, **modes):
    ss.update_preferences(s.conn, s.uid, {f"pay_{m}_enabled": on for m, on in modes.items()})


def item(s, sale, qty=1):
    line = s.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ?", (sale,)).fetchone()["id"]
    return [{"sale_line_id": line, "qty": qty, "reason": "wrong_size"}]


# ---------------------------------------------------------------- the list of accepted modes

def test_every_mode_is_accepted_until_the_owner_switches_one_off(s):
    assert enabled_payment_modes(s.conn) == ACCEPTED_PAYMENT_MODES


def test_switching_a_mode_off_removes_only_that_mode(s):
    switch(s, upi=False, cheque=False)
    assert enabled_payment_modes(s.conn) == ("cash", "card", "bank_transfer", "other")
    switch(s, upi=True)
    assert enabled_payment_modes(s.conn) == ("cash", "upi", "card", "bank_transfer", "other")


def test_cash_cannot_be_switched_off(s):
    with pytest.raises(ValueError, match="Unknown setting"):
        ss.update_preferences(s.conn, s.uid, {"pay_cash_enabled": False})
    switch(s, upi=False, card=False, bank_transfer=False, cheque=False, other=False)
    assert enabled_payment_modes(s.conn) == ("cash",)
    sell(s)  # still works


def test_the_settings_list_the_five_switches_in_their_own_group(s):
    prefs = ss.get_preferences(s.conn)
    keys = [k for k, p in prefs.items() if p["group"] == "Payments"]
    assert keys == ["pay_upi_enabled", "pay_card_enabled", "pay_bank_transfer_enabled", "pay_cheque_enabled", "pay_other_enabled"]
    assert all(prefs[k]["kind"] == "bool" and prefs[k]["value"] is True for k in keys)


def test_only_someone_allowed_to_change_settings_can_switch_modes(s):
    with pytest.raises(PermissionDenied):
        ss.update_preferences(s.conn, s.users["till"], {"pay_upi_enabled": False})
    assert "upi" in enabled_payment_modes(s.conn)


# ---------------------------------------------------------------- selling

def test_a_switched_off_mode_is_refused_on_a_bill_and_nothing_is_saved(s):
    switch(s, upi=False)
    before_stock, before_sales = get_stock(s.conn, s.variant), s.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    with pytest.raises(ValueError, match="Payment mode 'upi' is not available") as error:
        sell(s, "upi")
    assert "upi" not in str(error.value).split("Use one of:")[1]
    assert get_stock(s.conn, s.variant) == before_stock
    assert s.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == before_sales
    sell(s, "card")  # the others still work


def test_one_switched_off_part_of_a_split_payment_refuses_the_whole_bill(s):
    switch(s, cheque=False)
    with pytest.raises(ValueError, match="'cheque' is not available"):
        create_sale(s.conn, [{"variant_id": s.variant, "qty": 1}],
                    [{"mode": "cash", "amount_paise": 50000}, {"mode": "cheque", "amount_paise": PRICE - 50000}], user_id=s.uid)
    assert s.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def test_bills_already_saved_keep_their_mode_after_it_is_switched_off(s):
    sale = sell(s, "upi")
    switch(s, upi=False)
    modes = [p["mode"] for p in get_sale(s.conn, sale)["payments"]]
    assert modes == ["upi"]


# ---------------------------------------------------------------- refunds and exchanges

def test_a_refund_method_chosen_by_the_owner_must_be_accepted(s):
    sale = sell(s, "cash")
    switch(s, bank_transfer=False)
    with pytest.raises(ValueError, match="Refund mode 'bank_transfer' is not available"):
        process_return(s.conn, sale, item(s, sale), user_id=s.uid,
                       outcome="refund", refund_mode="bank_transfer")
    assert s.conn.execute("SELECT COUNT(*) FROM sale_returns").fetchone()[0] == 0


def test_a_refund_to_the_original_method_still_works_after_it_is_switched_off(s):
    sale = sell(s, "upi")
    switch(s, upi=False)
    credit_note = process_return(s.conn, sale, item(s, sale), user_id=s.uid, outcome="refund")
    assert credit_note
    paid_back = s.conn.execute("SELECT mode, amount_paise FROM payments WHERE amount_paise < 0").fetchall()
    assert [(p["mode"], p["amount_paise"]) for p in paid_back] == [("upi", -PRICE)]


def test_an_exchange_extra_payment_must_use_an_accepted_mode(s):
    sale = sell(s, "cash")
    switch(s, card=False)
    with pytest.raises(ValueError, match="'card' is not available"):
        process_return(s.conn, sale, item(s, sale), user_id=s.uid, outcome="exchange",
                       exchange_cart=[{"variant_id": s.variant, "qty": 2}],
                       payments=[{"mode": "card", "amount_paise": PRICE}])
    assert s.conn.execute("SELECT COUNT(*) FROM sale_returns").fetchone()[0] == 0


# ---------------------------------------------------------------- money received from customers later

def test_a_customer_payment_receipt_follows_the_same_switches(s):
    switch(s, cheque=False)
    with pytest.raises(ValueError, match="'cheque' is not available"):
        receive_payment(s.conn, amount_paise=1000, mode="cheque", user_id=s.uid)
    with pytest.raises(ValueError, match="Select a customer or a bill"):  # an accepted mode gets past the mode check
        receive_payment(s.conn, amount_paise=1000, mode="upi", user_id=s.uid)


# ---------------------------------------------------------------- the screens

def _owner_session(s):
    return shop.open_shop(s.folder, "owner", "owner-password")


def test_the_payment_window_offers_only_accepted_modes(s):
    from ui.billing_counter_view import PaymentDialog

    switch(s, upi=False, other=False)
    dialog = PaymentDialog(PRICE, lambda payments: (True, ""), modes=enabled_payment_modes(s.conn))
    combo = dialog.rows[0][0]
    offered = [combo.itemData(i) for i in range(combo.count())]
    assert offered == ["cash", "card", "bank_transfer", "cheque"]


def test_the_returns_screen_offers_only_accepted_modes(s):
    from ui.sales_returns_view import SalesReturnsView

    switch(s, upi=False)
    conn, session = _owner_session(s)
    view = SalesReturnsView(conn, session)
    for combo in (view.cmb_refund_mode, view.cmb_pay_mode):
        modes = [combo.itemData(i) for i in range(combo.count())]
        assert "upi" not in modes and "cash" in modes
    assert view.cmb_refund_mode.itemData(0) is None  # "Same as the original payment" stays first
    conn.close()


def test_the_settings_screen_shows_the_switches_and_saves_them(s):
    from ui.screens.settings_screen import SettingsScreen

    conn, session = _owner_session(s)
    view = SettingsScreen(conn, session)
    view._info = view._error = lambda title, text: None
    kind, box = view._pref_widgets["pay_cheque_enabled"]
    assert kind == "bool" and box.isChecked()
    box.setChecked(False)
    view.save_preferences()
    assert "cheque" not in enabled_payment_modes(conn)
    conn.close()
