# core/matrix.py
import re
from typing import List, Dict, Any

def generate_sku(product_code: str, size: str, colour: str) -> str:
    """Generates unique internal SKU from Product Code, Size, and Colour."""
    clean_p = re.sub(r'[^A-Z0-9]', '', product_code.upper())
    clean_s = re.sub(r'[^A-Z0-9]', '', size.upper())
    clean_c = re.sub(r'[^A-Z0-9]', '', colour.upper())
    return f"{clean_p}-{clean_s}-{clean_c}"

def generate_ean13_barcode(product_id: int, variant_index: int) -> str:
    """Generates a standard 13-digit internal barcode with checksum."""
    prefix = "200"  # Internal use prefix
    payload = f"{product_id:05d}{variant_index:04d}"
    raw = prefix + payload
    
    # EAN-13 Check digit calculation
    odd_sum = sum(int(raw[i]) for i in range(0, 12, 2))
    even_sum = sum(int(raw[i]) for i in range(1, 12, 2))
    total = odd_sum + (even_sum * 3)
    check_digit = (10 - (total % 10)) % 10
    
    return f"{raw}{check_digit}"

def generate_matrix_variants(
    product_id: int,
    product_code: str,
    sizes: List[str],
    colours: List[str],
    default_price: float,
    unit: str = 'piece',
    min_cut_length: float = 0.0
) -> List[Dict[str, Any]]:
    """
    Creates a size x colour matrix grid (e.g. 5 sizes x 6 colours = 30 variants).
    Generates unique SKU, barcode, and initial stock record per cell.
    """
    matrix_variants = []
    variant_idx = 1
    
    for colour in colours:
        for size in sizes:
            sku = generate_sku(product_code, size, colour)
            barcode = generate_ean13_barcode(product_id, variant_idx)
            
            matrix_variants.append({
                "product_id": product_id,
                "size": size.strip(),
                "colour": colour.strip(),
                "sku": sku,
                "barcode": barcode,
                "price": default_price,
                "unit": unit,
                "min_cut_milli": int(min_cut_length * 1000)
            })
            variant_idx += 1
            
    return matrix_variants


class MatrixEngine:
    """Compatibility wrapper for UI views."""
    def __init__(self):
        pass

    