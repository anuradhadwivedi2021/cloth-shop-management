"""Showing and reading money on screens and bills. All amounts are stored as whole paise (integers)."""

import re


def format_rupees(paise, *, symbol: str = "\u20b9") -> str:
    """12345678 -> '₹1,23,456.78' (Indian digit grouping). Negative amounts: '-₹1,500.50'."""
    if not isinstance(paise, int) or isinstance(paise, bool):
        raise ValueError("Money must be a whole number of paise.")
    sign = "-" if paise < 0 else ""
    rupees, part = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}{symbol}{digits}.{part:02d}"


_NUMBER = re.compile(r"^\d+(\.\d+)?$")


def parse_rupees(text, *, allow_zero: bool = True) -> int:
    """Rupees typed on a screen -> whole paise, exactly (never through float).

    '1,050.50' and '\u20b91050.5' -> 105050.  Floats are wrong for money
    (int(float('19.99') * 100) is 1998), so this works on the digits only.
    Raises ValueError with a message that is safe to show the user.
    """
    if not isinstance(text, str):
        raise ValueError("Enter the amount as text, for example 1050.50.")
    cleaned = text.replace("\u20b9", "").replace(",", "").strip()
    if not _NUMBER.match(cleaned):
        raise ValueError("Enter a valid amount, for example 1050.50.")
    whole, _, fraction = cleaned.partition(".")
    if len(fraction) > 2:
        raise ValueError("An amount can have at most 2 decimal places (paise).")
    if len(whole) > 12:
        raise ValueError("That amount is too large.")
    paise = int(whole) * 100 + int((fraction + "00")[:2])
    if paise == 0 and not allow_zero:
        raise ValueError("Amount must be more than zero.")
    return paise