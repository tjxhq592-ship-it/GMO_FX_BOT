# -*- coding: utf-8 -*-
"""
Dukascopy データフェッチャー
https://pypi.org/project/dukascopy-python/

対応通貨ペア: USD_JPY / EUR_JPY / GBP_JPY / AUD_JPY / NZD_JPY / CAD_JPY / CHF_JPY
             EUR_USD / GBP_USD / AUD_USD / NZD_USD
対応時間足  : 30min / 1hour / 4hour / 1day
"""
import pickle
import os
import time
import hashlib
from datetime import datetime

import pandas as pd

try:
    import dukascopy_python
    from dukascopy_python.instruments import (
        INSTRUMENT_FX_MAJORS_USD_JPY,
        INSTRUMENT_FX_MAJORS_EUR_JPY,
        INSTRUMENT_FX_MAJORS_GBP_JPY,
        INSTRUMENT_FX_MAJORS_AUD_JPY,
        INSTRUMENT_FX_MAJORS_NZD_JPY,
        INSTRUMENT_FX_MAJORS_CAD_JPY,
        INSTRUMENT_FX_MAJORS_CHF_JPY,
        INSTRUMENT_FX_MAJORS_EUR_USD,
        INSTRUMENT_FX_MAJORS_GBP_USD,
        INSTRUMENT_FX_MAJORS_AUD_USD,
        INSTRUMENT_FX_MAJORS_NZD_USD,
    )
    _DUKASCOPY_AVAILABLE = True
except ImportError:
    _DUKASCOPY_AVAILABLE = False

# ── シンボルマッピング ────────────────────────────────────────────────────
SYMBOL_MAP: dict = {}
if _DUKASCOPY_AVAILABLE:
    SYMBOL_MAP = {
        "USD_JPY": INSTRUMENT_FX_MAJORS_USD_JPY,
        "EUR_JPY": INSTRUMENT_FX_MAJORS_EUR_JPY,
        "GBP_JPY": INSTRUMENT_FX_MAJORS_GBP_JPY,
        "AUD_JPY": INSTRUMENT_FX_MAJORS_AUD_JPY,
        "NZD_JPY": INSTRUMENT_FX_MAJORS_NZD_JPY,
        "CAD_JPY": INSTRUMENT_FX_MAJORS_CAD_JPY,
        "CHF_JPY": INSTRUMENT_FX_MAJORS_CHF_JPY,
        "EUR_USD": INSTRUMENT_FX_MAJORS_EUR_USD,
        "GBP_USD": INSTRUMENT_FX_MAJORS_GBP_USD,
        "AUD_USD": INSTRUMENT_FX_MAJORS_AUD_USD,
        "NZD_USD": INSTRUMENT_FX_MAJORS_NZD_USD,
    }

# ── 時間足マッピング ──────────────────────────────────────────────────────
def _interval_const(interval: str):
    """interval 文字列を dukascopy_python の定数に変換する"""
    if not _DUKASCOPY_AVAILABLE:
        raise ImportError("dukascopy-python がインストールされていません")
    mapping = {
        "30min": dukascopy_python.INTERVAL_MIN_30,
        "1hour": dukascopy_python.INTERVAL_HOUR_1,
        "4hour": dukascopy_python.INTERVAL_HOUR_4,
        "1day":  dukascopy_python.INTERVAL_DAY_1,
    }
    if interval not in mapping:
        raise ValueError(
            f"Dukascopy未対応の時間足: {interval}  "
            f"（対応: {list(mapping.keys())}）"
        )
    return mapping[interval]


# ── キャッシュ設定 ────────────────────────────────────────────────────────
CACHE_BASE = os.path.join(".cache", "dukascopy")
CACHE_TTL  = 24 * 3600  # 24時間（Dukascopyデータは変わらない）


def _cache_path(symbol: str, interval: str, start_year: int) -> str:
    key = f"dukascopy_{symbol}_{interval}_{start_year}"
    os.makedirs(CACHE_BASE, exist_ok=True)
    return os.path.join(CACHE_BASE, hashlib.md5(key.encode()).hexdigest() + ".pkl")


# ── メイン取得関数 ────────────────────────────────────────────────────────
def fetch_dukascopy(
    symbol: str,
    interval: str = "30min",
    start_year: int = 2016,
    end_year: int | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Dukascopy から履歴データを取得して DataFrame で返す。

    Parameters
    ----------
    symbol      : GMO 形式の通貨ペア文字列（例: "USD_JPY"）
    interval    : 時間足（"30min" / "1hour" / "4hour" / "1day"）
    start_year  : 取得開始年（デフォルト 2016）
    end_year    : 取得終了年（None = 今年）
    use_cache   : True = pickle キャッシュを使用

    Returns
    -------
    pd.DataFrame  columns: Open / High / Low / Close / Volume
                  index  : openTime (tz-naive)
    """
    if not _DUKASCOPY_AVAILABLE:
        raise ImportError(
            "dukascopy-python がインストールされていません。\n"
            "pip install dukascopy-python を実行してください。"
        )

    if symbol not in SYMBOL_MAP:
        raise ValueError(
            f"{symbol} は Dukascopy 未対応です。\n"
            f"対応ペア: {list(SYMBOL_MAP.keys())}"
        )

    # キャッシュ確認
    cache_path = _cache_path(symbol, interval, start_year)
    if use_cache and os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < CACHE_TTL:
            with open(cache_path, "rb") as f:
                return pickle.load(f)

    end_y  = end_year or datetime.now().year
    start  = datetime(start_year, 1, 1)
    end    = datetime(end_y, 12, 31)

    df = dukascopy_python.fetch(
        instrument=SYMBOL_MAP[symbol],
        interval=_interval_const(interval),
        offer_side=dukascopy_python.OFFER_SIDE_BID,
        start=start,
        end=end,
    )

    if df is None or df.empty:
        raise ValueError(f"{symbol}: Dukascopy からデータ取得失敗")

    # 列名を backtesting ライブラリ用に大文字化
    df.columns = [c.capitalize() for c in df.columns]

    # タイムゾーン除去
    if df.index.tz is not None:
        df.index = df.index.tz_convert(None)

    df.index.name = "openTime"

    # OHLC のみで dropna（FX は Volume が NaN）
    if "Volume" not in df.columns:
        df["Volume"] = float("nan")
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    df = df.dropna(subset=["Open", "High", "Low", "Close"])

    # キャッシュ保存
    if use_cache:
        with open(cache_path, "wb") as f:
            pickle.dump(df, f)

    return df
