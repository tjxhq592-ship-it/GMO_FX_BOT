from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import hashlib
import os
import pandas as pd
import pickle
import json
import time
import yfinance as yf
from utils import calculate_rsi as _calculate_rsi

# backtesting.py が numpy 配列を期待するためラップ
def calculate_rsi(prices, period=14):
    result = _calculate_rsi(pd.Series(prices), period=period)
    return result.values if hasattr(result, "values") else result

# === 設定 ===
START_DATE = datetime(2022, 1, 1)
END_DATE = datetime.now() - timedelta(days=1)
INITIAL_CASH = 1_000_000  # 円
WF_TRAIN_MONTHS = 12
WF_TEST_MONTHS = 3
CACHE_DIR = ".cache"
CACHE_TTL = 12 * 3600  # 12時間（秒）
PARAMS_FILE = "params.json"

# FXシンボルマッピング（GMO FX → Yahoo Finance）
SYMBOLS = ["USD_JPY", "EUR_JPY", "GBP_JPY"]
SYMBOL_MAP = {
    "USD_JPY": "USDJPY=X",
    "EUR_JPY": "EURJPY=X",
    "GBP_JPY": "GBPJPY=X",
}

# === データキャッシュ ===
def _cache_path(symbol, start, end):
    key = f"{symbol}_{start.date()}_{end.date()}"
    return os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".pkl")

def get_historical_data(symbol):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol, START_DATE, END_DATE)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        with open(path, "rb") as f:
            print(f"  {symbol} キャッシュから読み込み")
            return pickle.load(f)

    yf_symbol = SYMBOL_MAP[symbol]
    df = yf.download(
        yf_symbol,
        start=START_DATE.strftime("%Y-%m-%d"),
        end=END_DATE.strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=True,
        progress=False,
    )
    if df.empty:
        raise ValueError(f"{symbol} ({yf_symbol}): データ取得失敗")

    # MultiIndex の場合は1段目を除去
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

    with open(path, "wb") as f:
        pickle.dump(df, f)
    return df

# === 戦略 ===
class ImprovedStrategy(Strategy):
    ma_short = 13
    ma_long = 15
    rsi_upper = 75
    rsi_lower = 25
    stop_loss_pct = 0.98
    take_profit_pct = 1.10
    trade_size = 0.2

    def init(self):
        close = self.data.Close
        self.ma_s = self.I(
            lambda x: pd.Series(x).ewm(span=self.ma_short, adjust=False).mean().values,
            close
        )
        self.ma_l = self.I(
            lambda x: pd.Series(x).ewm(span=self.ma_long, adjust=False).mean().values,
            close
        )
        self.rsi = self.I(calculate_rsi, close)

    def next(self):
        price = self.data.Close[-1]
        sl = price * self.stop_loss_pct
        tp = price * self.take_profit_pct

        if crossover(self.ma_s, self.ma_l) and self.rsi[-1] < self.rsi_upper:
            if not self.position:
                self.buy(size=self.trade_size, sl=sl, tp=tp)

        elif crossover(self.ma_l, self.ma_s) and self.rsi[-1] > self.rsi_lower:
            if self.position:
                self.position.close()

# === ウォークフォワードテスト ===
def walk_forward_test(symbol, data, params_dict):
    wft_start = END_DATE - relativedelta(months=WF_TEST_MONTHS)
    test_data = data[data.index >= wft_start]

    if len(test_data) < 20:
        print(f"  {symbol} WFT: テストデータ不足({len(test_data)}件)")
        return None

    bt = Backtest(test_data, ImprovedStrategy, cash=INITIAL_CASH, commission=0.00002)
    stats = bt.run(**params_dict)
    return stats['Sharpe Ratio']

# === 1銘柄分の最適化処理（並列ワーカー用）===
def optimize_symbol(args):
    symbol, wft_cutoff, _ = args

    data = get_historical_data(symbol)
    train_data = data[data.index < wft_cutoff]

    bt = Backtest(train_data, ImprovedStrategy, cash=INITIAL_CASH, commission=0.00002)
    stats = bt.optimize(
        ma_short=range(3, 15, 2),
        ma_long=range(10, 50, 5),
        rsi_upper=range(60, 80, 5),
        rsi_lower=range(20, 40, 5),
        stop_loss_pct=[0.95, 0.97, 0.98],
        take_profit_pct=[1.04, 1.06, 1.08, 1.10],
        maximize="Sharpe Ratio",
        constraint=lambda p: p.ma_short < p.ma_long
    )

    p = stats._strategy
    params_dict = {
        "ma_short": int(p.ma_short),
        "ma_long": int(p.ma_long),
        "rsi_upper": int(p.rsi_upper),
        "rsi_lower": int(p.rsi_lower),
        "stop_loss_pct": float(p.stop_loss_pct),
        "take_profit_pct": float(p.take_profit_pct)
    }
    is_sharpe = stats['Sharpe Ratio']
    wft_sharpe = walk_forward_test(symbol, data, params_dict)

    # エクイティカーブ（全期間で再実行して取得）
    bt_full = Backtest(data, ImprovedStrategy, cash=INITIAL_CASH, commission=0.00002)
    stats_full = bt_full.run(**params_dict)
    equity_curve = stats_full['_equity_curve']['Equity']
    equity_dates  = equity_curve.index.strftime("%Y-%m-%d").tolist()
    equity_values = equity_curve.tolist()

    return {
        "symbol": symbol,
        "params_dict": params_dict,
        "is_sharpe": is_sharpe,
        "wft_sharpe": wft_sharpe,
        "equity_final": stats['Equity Final [$]'],
        "return_pct": stats['Return [%]'],
        "return_ann": stats['Return (Ann.) [%]'],
        "max_dd": stats['Max. Drawdown [%]'],
        "win_rate": stats['Win Rate [%]'],
        "n_trades": stats['# Trades'],
        "equity_dates":  equity_dates,
        "equity_values": equity_values,
    }

# === 前回パラメータ読み込み ===
def load_prev_params():
    try:
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("params", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

# === パラメータ急変チェック ===
PARAM_LIMITS = {
    "ma_short":        0.50,
    "ma_long":         0.50,
    "rsi_upper":       0.15,
    "rsi_lower":       0.15,
    "stop_loss_pct":   0.05,
    "take_profit_pct": 0.10,
}

def check_param_change(symbol, new_params, prev_params):
    if symbol not in prev_params:
        return new_params

    prev = prev_params[symbol]
    adjusted = dict(new_params)

    for key, limit in PARAM_LIMITS.items():
        if key not in prev or key not in adjusted:
            continue
        prev_val = prev[key]
        new_val = adjusted[key]
        if prev_val == 0:
            continue
        change_rate = abs(new_val - prev_val) / abs(prev_val)
        if change_rate > limit:
            print(f"  ⚠️ {key}: {prev_val} → {new_val} (変化率{change_rate*100:.1f}% > {limit*100:.0f}%) → 前回値を維持")
            adjusted[key] = prev_val

    return adjusted

if __name__ == "__main__":
    prev_params = load_prev_params()
    wft_cutoff = END_DATE - relativedelta(months=WF_TEST_MONTHS)

    print(f"順次処理開始: {len(SYMBOLS)}銘柄")

    raw_results = {}

    for symbol in SYMBOLS:
        try:
            result = optimize_symbol((symbol, wft_cutoff, prev_params))
            raw_results[symbol] = result
            wft_str = f"{result['wft_sharpe']:.2f}" if result['wft_sharpe'] is not None else "N/A"
            print(f"\n{symbol} 完了: シャープ(IS)={result['is_sharpe']:.2f}  シャープ(WFT)={wft_str}")
        except Exception as e:
            print(f"\n{symbol} エラー: {e}")

    # === 結果集計（SYMBOLS順に整列）===
    results = []
    equity_finals = []

    for symbol in SYMBOLS:
        if symbol not in raw_results:
            continue
        r = raw_results[symbol]
        wft_str = f"{r['wft_sharpe']:.2f}" if r['wft_sharpe'] is not None else "N/A"
        equity_finals.append(r['equity_final'])
        results.append({
            "銘柄": symbol,
            "最終資産": f"¥{r['equity_final']:,.0f}",
            "総リターン": f"{r['return_pct']:.1f}%",
            "年率リターン": f"{r['return_ann']:.1f}%",
            "最大DD": f"{r['max_dd']:.1f}%",
            "勝率": f"{r['win_rate']:.1f}%",
            "取引回数": r['n_trades'],
            "シャープ(IS)": f"{r['is_sharpe']:.2f}",
            "シャープ(WFT)": wft_str,
            "_is_sharpe": r['is_sharpe'],
            "_wft_sharpe": r['wft_sharpe'],
        })

    # === 結果表示 ===
    print("\n=== 全銘柄最適化バックテスト結果 ===")
    print(f"最適化期間: {START_DATE.date()} 〜 {wft_cutoff.date()}  WFT期間: {wft_cutoff.date()} 〜 {END_DATE.date()}")
    print(f"初期資金（各銘柄）: ¥{INITIAL_CASH:,}")
    print()

    display_cols = ["銘柄", "最終資産", "総リターン", "年率リターン", "最大DD", "勝率", "取引回数", "シャープ(IS)", "シャープ(WFT)"]
    df = pd.DataFrame(results)[display_cols]
    print(df.to_string(index=False))

    total_final = sum(equity_finals)
    total_initial = INITIAL_CASH * len(equity_finals)
    total_return = (total_final - total_initial) / total_initial * 100

    print(f"\n=== 合計 ===")
    print(f"総投資額: ¥{total_initial:,}")
    print(f"最終資産合計: ¥{total_final:,.0f}")
    print(f"総合リターン: {total_return:.1f}%")

    # === 最適パラメータ一覧 ===
    print("\n=== 銘柄別最適パラメータ一覧 ===")
    for symbol in SYMBOLS:
        if symbol in raw_results:
            print(f"{symbol}: {raw_results[symbol]['params_dict']}")

    # === JSONに保存 ===
    SHARPE_THRESHOLD = 1.0
    results_dict = {r["銘柄"]: r for r in results}
    params_out = {}
    excluded = []

    for symbol in SYMBOLS:
        if symbol not in raw_results:
            continue
        r = results_dict[symbol]
        is_sharpe = r["_is_sharpe"]
        wft_sharpe = r["_wft_sharpe"]
        new_p = raw_results[symbol]['params_dict']

        if wft_sharpe is not None and wft_sharpe < 0:
            excluded.append(f"{symbol}(WFTシャープ:{wft_sharpe:.2f})")
            print(f"⚠️ {symbol} はWFTシャープ{wft_sharpe:.2f}のため除外")
            continue

        if is_sharpe >= SHARPE_THRESHOLD:
            print(f"{symbol} パラメータ急変チェック中...")
            adjusted_p = check_param_change(symbol, new_p, prev_params)
            params_out[symbol] = adjusted_p
        else:
            excluded.append(f"{symbol}(シャープ:{is_sharpe:.2f})")
            print(f"⚠️ {symbol} はシャープレシオ{is_sharpe:.2f}のため除外")

    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": params_out,
        "excluded": excluded
    }

    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nparams.jsonに保存しました！")

    # === backtest_results.json に出力 ===
    RESULTS_FILE = "backtest_results.json"
    bt_results = {}
    for symbol in SYMBOLS:
        if symbol not in raw_results:
            continue
        r = raw_results[symbol]
        bt_results[symbol] = {
            "wft_sharpe": r["wft_sharpe"],
            "max_dd":     r["max_dd"],
            "dates":      r.get("equity_dates", []),
            "equity":     r.get("equity_values", []),
        }

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(bt_results, f, indent=2, ensure_ascii=False)

    print(f"backtest_results.jsonに保存しました！")
