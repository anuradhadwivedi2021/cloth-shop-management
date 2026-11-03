# core/units.py
from decimal import Decimal, ROUND_HALF_UP

UNIT_PIECE = "piece"
UNIT_PAIR = "pair"
UNIT_SET = "set"
UNIT_METRE = "metre"

INTEGER_UNITS = {UNIT_PIECE, UNIT_PAIR, UNIT_SET}

def parse_qty_to_milli(qty_val: str | float | int | Decimal, unit: str) -> int:
    """
    Converts user input quantity into internal integer milli-units.
    - Metre: 1.250 -> 1250
    - Piece/Pair/Set: Must be whole numbers (1 -> 1000)
    """
    unit_clean = (unit or "").strip().lower()
    d_qty = Decimal(str(qty_val))
    
    if unit_clean in INTEGER_UNITS:
        if d_qty % 1 != 0:
            raise ValueError(f"Fractional quantities are not allowed for '{unit}'. Must be a whole number.")
        return int(d_qty * 1000)
    
    if unit_clean == UNIT_METRE:
        # Quantities in metre support up to 3 decimal places
        milli = int((d_qty * Decimal('1000')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        return milli

    # Default fallback for integer unit types
    return int(d_qty * 1000)

def format_milli_to_qty(milli_qty: int, unit: str) -> str:
    """
    Formats internal integer milli-units for UI & Bill displays.
    - Metre: 1250 -> 1.250 m
    - Piece: 1000 -> 1
    """
    unit_clean = (unit or "").strip().lower()
    d_milli = Decimal(milli_qty) / Decimal(1000)
    
    if unit_clean in INTEGER_UNITS:
        return str(int(d_milli))
    elif unit_clean == UNIT_METRE:
        return f"{d_milli:.3f}"
    return f"{d_milli:.3f}"

def validate_cut_length(milli_qty: int, min_cut_milli: int | None, unit: str):
    """
    Validates minimum cut length for metre fabric items.
    """
    if (unit or "").strip().lower() == UNIT_METRE and min_cut_milli:
        if milli_qty < min_cut_milli:
            req_m = format_milli_to_qty(min_cut_milli, UNIT_METRE)
            raise ValueError(f"Selected length is below minimum cut length of {req_m} m.")