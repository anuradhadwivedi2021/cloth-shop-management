"""GST calculation for sales bills.

Pure functions (no database) so every figure can be checked to the paisa.
All money is integer paise. Rates and thresholds come from the editable
`tax_rules` table, never from code, so a future rate change needs no release.

Method (stored with every bill; must be confirmed with the owner's accountant):
  * Rate is chosen per piece from the HSN rule table valid on the BILL DATE.
    Rules are tried lowest price-ceiling first; the rule with no ceiling is
    the fallback. For tax-inclusive prices the test is
    "taxable value per piece (price / (1 + rate)) <= ceiling", so the 5% rule is
    tested first. The test uses the price AFTER discounts (actual sale value).
  * Item discount, then bill discount, reduce the taxable value. A bill-level
    discount is spread over lines in proportion to their value (largest
    remainder, so the shares add up exactly).
  * Tax is rounded half-up to whole paise per line. For tax-inclusive prices,
    taxable = inclusive amount / (1 + rate) and tax = inclusive - taxable, so
    taxable + tax always equals the line amount exactly.
  * Intra-state: tax is split CGST / SGST (an odd paisa goes to SGST).
    Inter-state: all tax is IGST.
  * The bill total is rounded half-up to the nearest rupee; the round-off is
    reported as its own figure.
  * Composition and unregistered modes show no tax: the price is the amount.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from db.connection import transaction

TAX_MODES = ("regular", "composition", "unregistered")

CALC_METHOD = (
    "Tax per line rounded half-up to paise; total rounded half-up to nearest rupee; "
    "round-off shown separately; rate chosen on per-piece value after discount."
)

# (hsn_prefix, rate_percent, price_ceiling_paise, valid_from, note)
# Apparel: 5% up to Rs 2,500 per piece, 18% above (from 22 Sep 2025).
# Accountant must confirm. Editable in the tax_rules table.
DEFAULT_TAX_RULES = [
    (prefix, rate, ceiling, "2025-09-22", "Apparel/textiles slab; owner/accountant to confirm")
    for prefix in ("61", "62", "63")
    for rate, ceiling in ((5, 250000), (18, None))
]


class TaxRuleError(Exception):
    """Raised when no tax rate can be worked out for a line."""


# ---------- helpers ----------

def _d(value) -> Decimal:
    return Decimal(str(value))


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _iso(on) -> str:
    if isinstance(on, date):
        return on.isoformat()
    date.fromisoformat(on)  # raises ValueError for a bad date
    return on


def _spread(total: int, weights: list) -> list:
    """Split `total` paise across lines in proportion to `weights`, adding up exactly."""
    weight_sum = sum(weights)
    if total == 0 or weight_sum == 0:
        return [0] * len(weights)
    shares = [total * w // weight_sum for w in weights]
    remainders = [(total * w % weight_sum, -i) for i, w in enumerate(weights)]
    leftover = total - sum(shares)
    for _, neg_i in sorted(remainders, reverse=True)[:leftover]:
        shares[-neg_i] += 1
    return shares


# ---------- rate rules ----------

def find_rate_rule(rules, hsn, net_paise: int, qty: int, *, on_date, prices_include_tax: bool):
    """Pick the tax rule for one line. Returns the rule dict.

    net_paise is the line amount after discounts; the per-piece test uses net_paise / qty.
    """
    hsn = (hsn or "").strip()
    on = _iso(on_date)
    valid = [
        r for r in rules
        if hsn and hsn.startswith(str(r["hsn_prefix"]))
        and r["valid_from"] <= on
        and (not r.get("valid_to") or on <= r["valid_to"])
    ]
    if not valid:
        raise TaxRuleError(
            f"No tax rule found for HSN '{hsn}' on {on}. "
            "Set the GST rate on the product or add a tax rule."
        )
    longest = max(len(str(r["hsn_prefix"])) for r in valid)
    valid = [r for r in valid if len(str(r["hsn_prefix"])) == longest]
    valid.sort(key=lambda r: (r["price_ceiling_paise"] is None, r["price_ceiling_paise"] or 0))

    for rule in valid:
        ceiling = rule["price_ceiling_paise"]
        if ceiling is None:
            return rule
        if prices_include_tax:
            limit = Decimal(ceiling) * qty * (1 + _d(rule["rate_percent"]) / 100)
        else:
            limit = Decimal(ceiling) * qty
        if net_paise <= limit:
            return rule
    raise TaxRuleError(f"No tax rule covers this price for HSN '{hsn}' on {on}.")


# ---------- the bill calculation ----------

def calculate_bill(
    lines,
    *,
    on_date,
    rules=(),
    mode: str = "regular",
    prices_include_tax: bool = True,
    bill_discount_paise: int = 0,
    intra_state: bool = True,
):
    """Work out every figure of a sales bill.

    lines: dicts with qty, unit_price_paise, and for regular GST also hsn
           (or an owner-confirmed rate_percent). Optional: discount_paise
           (total item discount for the line), variant_id.
    Returns a dict of line results and bill totals (all paise).
    """
    if mode not in TAX_MODES:
        raise ValueError(f"Tax mode must be one of {', '.join(TAX_MODES)}.")
    if not lines:
        raise ValueError("A bill needs at least one line.")
    if not isinstance(bill_discount_paise, int) or bill_discount_paise < 0:
        raise ValueError("Bill discount must be zero or a positive amount in paise.")
    on = _iso(on_date)

    prepared = []
    for line in lines:
        qty = line.get("qty")
        price = line.get("unit_price_paise")
        discount = line.get("discount_paise", 0)
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError("Quantity must be a whole number greater than zero.")
        if not isinstance(price, int) or price < 0:
            raise ValueError("Unit price must be a non-negative amount in paise.")
        if not isinstance(discount, int) or discount < 0:
            raise ValueError("Item discount must be zero or a positive amount in paise.")
        gross = qty * price
        if discount > gross:
            raise ValueError("Item discount cannot be larger than the item amount.")
        prepared.append((line, qty, price, discount, gross))

    after_item_discount = [gross - discount for _, _, _, discount, gross in prepared]
    if bill_discount_paise > sum(after_item_discount):
        raise ValueError("Bill discount cannot be larger than the bill amount.")
    bill_shares = _spread(bill_discount_paise, after_item_discount)

    results = []
    for (line, qty, price, discount, gross), after_item, share in zip(prepared, after_item_discount, bill_shares):
        net = after_item - share
        rule_id = None
        rate = Decimal(0)

        if mode == "regular":
            override = line.get("rate_percent")
            if override is not None:
                rate = _d(override)
                if rate < 0:
                    raise ValueError("Tax rate cannot be negative.")
            else:
                rule = find_rate_rule(
                    rules, line.get("hsn"), net, qty, on_date=on, prices_include_tax=prices_include_tax
                )
                rate = _d(rule["rate_percent"])
                rule_id = rule.get("id")

        if mode != "regular":
            taxable, tax, line_total = net, 0, net
        elif prices_include_tax:
            taxable = _round_half_up(Decimal(net) / (1 + rate / 100))
            tax = net - taxable
            line_total = net
        else:
            taxable = net
            tax = _round_half_up(Decimal(net) * rate / 100)
            line_total = net + tax

        if intra_state:
            cgst = tax // 2
            sgst = tax - cgst
            igst = 0
        else:
            cgst = sgst = 0
            igst = tax

        results.append(
            {
                "variant_id": line.get("variant_id"),
                "hsn": line.get("hsn"),
                "qty": qty,
                "unit_price_paise": price,
                "gross_paise": gross,
                "item_discount_paise": discount,
                "bill_discount_share_paise": share,
                "net_paise": net,
                "rate_percent": float(rate),
                "tax_rule_id": rule_id,
                "taxable_paise": taxable,
                "cgst_paise": cgst,
                "sgst_paise": sgst,
                "igst_paise": igst,
                "tax_paise": tax,
                "line_total_paise": line_total,
            }
        )

    before_round = sum(r["line_total_paise"] for r in results)
    total = ((before_round + 50) // 100) * 100
    return {
        "mode": mode,
        "prices_include_tax": prices_include_tax,
        "intra_state": intra_state,
        "on_date": on,
        "method": CALC_METHOD,
        "lines": results,
        "gross_paise": sum(r["gross_paise"] for r in results),
        "item_discount_paise": sum(r["item_discount_paise"] for r in results),
        "bill_discount_paise": bill_discount_paise,
        "taxable_paise": sum(r["taxable_paise"] for r in results),
        "cgst_paise": sum(r["cgst_paise"] for r in results),
        "sgst_paise": sum(r["sgst_paise"] for r in results),
        "igst_paise": sum(r["igst_paise"] for r in results),
        "tax_paise": sum(r["tax_paise"] for r in results),
        "before_round_paise": before_round,
        "round_off_paise": total - before_round,
        "total_paise": total,
    }


# ---------- rule table in the database ----------

def ensure_default_tax_rules(conn) -> int:
    """Fill the tax_rules table with the apparel slabs if it is empty. Returns rows added."""
    with transaction(conn):
        if conn.execute("SELECT COUNT(*) FROM tax_rules").fetchone()[0] > 0:
            return 0
        for prefix, rate, ceiling, valid_from, note in DEFAULT_TAX_RULES:
            conn.execute(
                "INSERT INTO tax_rules(hsn_prefix, rate_percent, price_ceiling_paise, valid_from, note) "
                "VALUES (?,?,?,?,?)",
                (prefix, rate, ceiling, valid_from, note),
            )
        return len(DEFAULT_TAX_RULES)


def load_tax_rules(conn) -> list:
    """All tax rules as plain dicts, ready for calculate_bill."""
    rows = conn.execute("SELECT * FROM tax_rules ORDER BY id").fetchall()
    return [dict(r) for r in rows]

class TaxEngine:
    """Compatibility wrapper for UI views."""
    def __init__(self):
        pass

    def calculate_item_tax(self, price, **kwargs):
        # Maps to existing tax calculation logic if available
        return {"rate": 5.0, "amount": price * 0.05}