"""Add a product with its size x colour grid from what was typed on the screen.

The screen only collects text; every rule (permission, prices as exact paise, duplicate
warning, audit log, one transaction per step) lives in core.product_edit.add_product.
"""

from core.money import parse_rupees
from core.product_edit import add_product

# Fabric sold by the metre (decimal quantities) is a later phase: whole quantities only for now.
SELLABLE_UNITS = ("piece", "pair", "set")


def split_list(text) -> list:
    """'S, M, l, M' -> ['S', 'M', 'l']  (blanks and case-insensitive repeats dropped, order kept)."""
    seen, result = set(), []
    for part in (text or "").split(","):
        item = part.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result


def preview_matrix(sizes_text, colours_text) -> list:
    """The (size, colour) combinations that will be created, in the order they are created."""
    sizes, colours = split_list(sizes_text), split_list(colours_text)
    if not sizes or not colours:
        raise ValueError("Enter at least one size and one colour.")
    return [(size, colour) for size in sizes for colour in colours]


def _optional_paise(text, label):
    if not (text or "").strip():
        return 0
    try:
        return parse_rupees(text)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None


def save_matrix_product(conn, user_id, *, name, category="", unit="piece", price_text="",
                        cost_text="", sizes_text="", colours_text="", allow_duplicate=False) -> dict:
    """Create the product and every size x colour variant. Returns {'product_id', 'variant_ids'}.

    Raises DuplicateProductError (nothing saved) if the name already exists and
    allow_duplicate is False, PermissionDenied if the user may not add products or set prices,
    and ValueError with a message that is safe to show for bad input.
    """
    if unit not in SELLABLE_UNITS:
        raise ValueError("Choose piece, pair or set.")
    combos = preview_matrix(sizes_text, colours_text)  # validates before anything is saved
    fields = {
        "default_price_paise": _optional_paise(price_text, "Selling price"),
        "default_cost_paise": _optional_paise(cost_text, "Cost price"),
        "unit": unit,
    }
    if (category or "").strip():
        fields["category"] = category.strip()
    result = add_product(
        conn, user_id, name,
        sizes=split_list(sizes_text), colours=split_list(colours_text),
        allow_duplicate=allow_duplicate, **fields,
    )
    assert len(result["variant_ids"]) == len(combos)
    return result
