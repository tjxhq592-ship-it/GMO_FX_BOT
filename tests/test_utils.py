import pandas as pd
import numpy as np
from utils import calculate_rsi, calculate_bollinger, calculate_atr


def test_calculate_rsi_basic():
    prices = pd.Series([1,2,3,4,5,6,7,8,9,10,11,12,13,14,15])
    rsi = calculate_rsi(prices, period=5)
    assert isinstance(rsi, pd.Series)
    # RSI should have NaN for the first periods
    assert rsi.isna().sum() >= 1


def test_bollinger_mid_equals_ma():
    prices = pd.Series([1.0,2.0,3.0,4.0,5.0,6.0,7.0])
    bb = calculate_bollinger(prices, period=3, std_mult=1)
    assert 'mid' in bb.columns
    # mid should equal rolling mean
    expected = prices.rolling(3).mean()
    expected.name = 'mid'
    pd.testing.assert_series_equal(bb['mid'], expected)


def test_atr_positive():
    high = pd.Series([1,2,3,4,5,6,7])
    low  = pd.Series([0.5,1.5,2.5,3.5,4.5,5.5,6.5])
    close= pd.Series([0.8,1.8,2.8,3.8,4.8,5.8,6.8])
    atr = calculate_atr(high, low, close, period=3)
    assert isinstance(atr, pd.Series)
    assert (atr.dropna() > 0).all()
