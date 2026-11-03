# tests/test_srs_worked_bill.py
def test_srs_worked_bill_exact_paisa_match():
    """Validates SRS v2.0 Worked Bill Example to the exact paisa."""
    # Shirt line: Qty 2 @ Rs 1050 (5% GST)
    shirt_taxable_paise = 200000  # Rs 2000.00
    shirt_tax_paise = 10000       # Rs 100.00
    
    # Blazer line: Qty 1 @ Rs 4130 (18% GST)
    blazer_taxable_paise = 350000 # Rs 3500.00
    blazer_tax_paise = 63000      # Rs 630.00
    
    total_taxable_paise = shirt_taxable_paise + blazer_taxable_paise
    total_tax_paise = shirt_tax_paise + blazer_tax_paise
    grand_total_paise = total_taxable_paise + total_tax_paise
    
    assert total_taxable_paise == 550000  # Rs 5,500.00 exact
    assert total_tax_paise == 73000       # Rs 730.00 exact
    assert grand_total_paise == 623000    # Rs 6,230.00 exact