"""
テクニカル指標ユーティリティ（Alpaca依存を除去済み）
"""
import pandas as pd


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_macd(
    prices: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    ema_fast    = prices.ewm(span=fast, adjust=False).mean()
    ema_slow    = prices.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line},
        index=prices.index,
    )


def calculate_bollinger(
    prices: pd.Series,
    period: int = 20,
    std_mult: int = 2,
) -> pd.DataFrame:
    mid   = prices.rolling(period).mean()
    sigma = prices.rolling(period).std()
    return pd.DataFrame(
        {"upper": mid + std_mult * sigma, "mid": mid, "lower": mid - std_mult * sigma},
        index=prices.index,
    )


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def calculate_volume_zscore(volume: pd.Series, period: int = 20) -> pd.Series:
    mean = volume.rolling(period).mean()
    std  = volume.rolling(period).std()
    return (volume - mean) / std.replace(0, float("nan"))


def get_market_condition(df: pd.DataFrame) -> str:
    """直近終値がEMA20より上なら bull、下なら bear"""
    closes = df["close"]
    ema20  = closes.ewm(span=20, adjust=False).mean()
    return "bull" if closes.iloc[-1] > ema20.iloc[-1] else "bear"


def calc_trade_size(cash_jpy: float, price: float, min_size: int = 1) -> int:
    """
    証拠金の5%を目安に取引数量を決定（1000通貨単位）
    GMO FXの最小取引単位は 1 = 1000通貨
    """
    budget    = max(1000.0, min(50000.0, cash_jpy * 0.05))
    lot_value = price * 1000  # 1ロットあたりの円換算
    size      = max(min_size, int(budget / lot_value))
    return size


def calculate_signal_score(
    ma_s: float,
    ma_l: float,
    macd_hist: float,
    rsi: float,
    price: float,
    bb_mid: float,
    vol_zscore: float,
    direction: int,
    vol_threshold: float = 1.5,
) -> int:
    """シグナルの強さを 0〜100 で数値化 / direction: +1=買い, -1=売り"""
    score = 0
    if direction == 1:
        if ma_s > ma_l:        score += 25
        if macd_hist > 0:      score += 20
        if 30 <= rsi <= 70:    score += 20
        if price < bb_mid:     score += 20
    else:
        if ma_s < ma_l:        score += 25
        if macd_hist < 0:      score += 20
        if 30 <= rsi <= 70:    score += 20
        if price > bb_mid:     score += 20
    if vol_zscore >= vol_threshold:
        score += 15
    return score
