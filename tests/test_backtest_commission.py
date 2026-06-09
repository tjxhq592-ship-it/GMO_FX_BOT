from backtest import calc_commission


def test_calc_commission_usdjpy():
    c = calc_commission("USD_JPY", 150.0)
    assert c > 0


def test_calc_commission_eurusd():
    c = calc_commission("EUR_USD", 1.63)
    assert c > 0


def test_calc_commission_price_zero():
    c = calc_commission("USD_JPY", 0)
    assert c == 0.00002
