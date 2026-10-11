from app.etl.data_builder import VAT_RATE, ex_vat


def test_ex_vat_divides_by_1_15():
    assert VAT_RATE == 0.15
    assert round(ex_vat(115.0), 2) == 100.0
    assert round(ex_vat(4_116_616.36)) == 3_579_666
    assert ex_vat(0) == 0
