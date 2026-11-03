import pytest

from core.money import format_rupees, parse_rupees


@pytest.mark.parametrize("paise, text", [
    (0, "\u20b90.00"),
    (99, "\u20b90.99"),
    (100, "\u20b91.00"),
    (99999, "\u20b9999.99"),
    (100000, "\u20b91,000.00"),
    (12345678, "\u20b91,23,456.78"),
    (100000000, "\u20b910,00,000.00"),
    (-150050, "-\u20b91,500.50"),
])
def test_rupees_use_indian_grouping(paise, text):
    assert format_rupees(paise) == text


@pytest.mark.parametrize("bad", [1.5, "100", None, True])
def test_money_must_be_whole_paise(bad):
    with pytest.raises(ValueError):
        format_rupees(bad)


@pytest.mark.parametrize("typed, paise", [
    ("1050", 105000),
    ("1,050.50", 105050),
    ("\u20b91050.5", 105050),
    (" 19.99 ", 1999),
    ("1.15", 115),
    ("0.29", 29),
    ("4.35", 435),
    ("1050.10", 105010),
    ("0", 0),
    ("0.05", 5),
    ("12,34,567.89", 123456789),
])
def test_typed_rupees_become_exact_paise(typed, paise):
    assert parse_rupees(typed) == paise  # the float way gives 1998 for 19.99


@pytest.mark.parametrize("bad", ["", "  ", "abc", "-5", "1.2.3", "1e3", "NaN", "inf", "1,05,0x", "19.999", ".5", "5.",
                                 "1" * 13, None, 19.99, 5])
def test_bad_amounts_are_refused_with_a_message(bad):
    with pytest.raises(ValueError):
        parse_rupees(bad)


def test_zero_can_be_refused():
    assert parse_rupees("0") == 0
    with pytest.raises(ValueError, match="more than zero"):
        parse_rupees("0.00", allow_zero=False)
    assert parse_rupees("0.01", allow_zero=False) == 1


def test_round_trip_through_format_rupees():
    for paise in (0, 1, 99, 100, 1999, 105050, 123456789):
        assert parse_rupees(format_rupees(paise)) == paise