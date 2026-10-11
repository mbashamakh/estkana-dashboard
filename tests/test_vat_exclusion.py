from app.etl.data_builder import VAT_RATE, ex_vat, standard_cost_pct_for


def test_ex_vat_divides_by_1_15():
    assert VAT_RATE == 0.15
    assert round(ex_vat(115.0), 2) == 100.0
    assert round(ex_vat(4_116_616.36)) == 3_579_666
    assert ex_vat(0) == 0


def test_standard_cost_uses_actual_ex_vat_sales():
    # chapati egg: recipe cost 0.75/unit. 100 sold for SAR 400 ex-VAT -> 75/400 = 18.75%
    assert standard_cost_pct_for({"شباتي بيض": (100, 400.0)}) == 18.75
    # item not on the recipe sheet is ignored; too little coverage -> company fallback
    assert standard_cost_pct_for({"شباتي بيض": (1, 4.0), "ثلاجة 1": (99, 500.0)}) == 41.48
