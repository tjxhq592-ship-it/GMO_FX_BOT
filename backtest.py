import matplotlib
matplotlib.use("Agg")

from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import hashlib
import logging
import warnings
import pandas as pd
import pickle
import json
import time
import yfinance as yf
from utils import calculate_rsi as _calculate_rsi

# ログ・警告の抑制
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

# backtesting.py が numpy 配列を期待するためラップ
def calculate_rsi(prices, period=14):
    result = _calculate_rsi(pd.Series(prices), period=period)
    return result.values if hasattr(result, "values") else result

# === 設定 ===
# yfinance 1h足は直近730日分のみ取得可能
START_DATE    = datetime.now() - timedelta(days=720)
END_DATE      = datetime.now() - timedelta(days=1)
FW_START_DATE = datetime.now() - timedelta(days=90)
FW_END_DATE   = datetime.now() - timedelta(days=1)

INITIAL_CASH    = 1_000_000  # 円
WF_TRAIN_MONTHS = 12         # 学習期間
WF_TEST_MONTHS  = 3          # 検証期間
MIN_TRADES      = 200        # バックテスト期間の最低取引回数
PF_THRESHOLD    = 1.2        # 最低 Profit Factor
SHARPE_THRESHOLD = 1.0

CACHE_DIR  = ".cache"
CACHE_TTL  = 3600  # 1時間足用（1時間）
PARAMS_FILE  = "params.json"
RESULTS_FILE = "backtest_results.json"

# FXシンボルマッピング（GMO FX → Yahoo Finance）
SYMBOLS = ["EUR_GBP", "AUD_NZD", "EUR_CHF"]
SYMBOL_MAP = {
    "EUR_GBP": "EURGBP=X",
    "AUD_NZD": "AUDNZD=X",
    "EUR_CHF": "EURCHF=X",
}

# === データキャッシュ ===
def _cache_path(symbol, start, end):
    key = f"{symbol}_{start.date()}_{end.date()}"
    return os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".pkl")

def _download(symbol, start, end):
    """yfinance からダウンロードして OHLCV DataFrame を返す"""
    yf_symbol = SYMBOL_MAP[symbol]
    df = yf.download(
        yf_symbol,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="1h",
        auto_adjust=True,
        progress=False,
    )
    if df.empty:
        raise ValueError(f"{symbol} ({yf_symbol}): データ取得失敗")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    idx = pd.to_datetime(df.index)
    df.index = idx.tz_convert(None) if idx.tz is not None else idx.tz_localize(None)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

def get_historical_data(symbol):
    """バックテスト期間（START_DATE〜END_DATE）のデータを取得"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol, START_DATE, END_DATE)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        with open(path, "rb") as f:
            return pickle.load(f)
    df = _download(symbol, START_DATE, END_DATE)
    with open(path, "wb") as f:
        pickle.dump(df, f)
    return df

def get_forward_data(symbol):
    """フォワードテスト期間（FW_START_DATE〜FW_END_DATE）のデータを取得"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol, FW_START_DATE, FW_END_DATE)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        with open(path, "rb") as f:
            return pickle.load(f)
    df = _download(symbol, FW_START_DATE, FW_END_DATE)
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

def _extract_stats(stats):
    """backtesting Stats オブジェクトから必要な指標を dict で返す"""
    pf = stats.get("Profit Factor", None)
    return {
        "sharpe":   float(stats["Sharpe Ratio"]),
        "pf":       float(pf) if pf is not None and str(pf) not in ("nan", "inf") else None,
        "max_dd":   float(stats["Max. Drawdown [%]"]),
        "win_rate": float(stats["Win Rate [%]"]),
        "n_trades": int(stats["# Trades"]),
        "return_pct": float(stats["Return [%]"]),
        "return_ann": float(stats["Return (Ann.) [%]"]),
        "equity_final": float(stats["Equity Final [$]"]),
    }

# === ウォークフォワードテスト ===
def walk_forward_test(data, params_dict):
    wft_start = END_DATE - relativedelta(months=WF_TEST_MONTHS)
    test_data = data[data.index >= wft_start]
    if len(test_data) < 20:
        return None
    bt = Backtest(test_data, ImprovedStrategy, cash=INITIAL_CASH, commission=0.00002)
    stats = bt.run(**params_dict)
    return _extract_stats(stats)

# === フォワードテスト ===
def run_forward_test(symbol, params_dict):
    """FW_START_DATE〜FW_END_DATE で最適パラメータをそのまま適用"""
    try:
        fw_data = get_forward_data(symbol)
    except Exception:
        return None
    if len(fw_data) < 20:
        return None
    bt = Backtest(fw_data, ImprovedStrategy, cash=INITIAL_CASH, commission=0.00002)
    stats = bt.run(**params_dict)
    return _extract_stats(stats)

# === 1銘柄分の最適化処理 ===
def optimize_symbol(symbol, idx, total, wft_cutoff, prev_params):
    tag = f"[{idx}/{total}] {symbol}"

    print(f"{tag} データ取得中...")
    data = get_historical_data(symbol)
    print(f"  データ: {len(data)}件 ({data.index[0]} 〜 {data.index[-1]})")

    train_data = data[data.index < wft_cutoff]
    print(f"{tag} 最適化中... (学習データ: {len(train_data)}件)")

    bt = Backtest(train_data, ImprovedStrategy, cash=INITIAL_CASH, commission=0.00002)
    stats = bt.optimize(
        ma_short=range(3, 20, 2),
        ma_long=range(10, 60, 5),
        rsi_upper=range(60, 80, 5),
        rsi_lower=range(20, 40, 5),
        stop_loss_pct=[0.95, 0.97, 0.98],
        take_profit_pct=[1.04, 1.06, 1.08, 1.10],
        maximize="Sharpe Ratio",
        constraint=lambda p: p.ma_short < p.ma_long
    )

    p = stats._strategy
    params_dict = {
        "ma_short":        int(p.ma_short),
        "ma_long":         int(p.ma_long),
        "rsi_upper":       int(p.rsi_upper),
        "rsi_lower":       int(p.rsi_lower),
        "stop_loss_pct":   float(p.stop_loss_pct),
        "take_profit_pct": float(p.take_profit_pct),
    }

    is_stats = _extract_stats(stats)
    print(f"{tag} WFTテスト中...")
    wft_result = walk_forward_test(data, params_dict)

    # エクイティカーブ（全バックテスト期間）
    print(f"{tag} エクイティカーブ生成中...")
    bt_full = Backtest(data, ImprovedStrategy, cash=INITIAL_CASH, commission=0.00002)
    stats_full = bt_full.run(**params_dict)
    equity_curve  = stats_full["_equity_curve"]["Equity"]
    equity_dates  = equity_curve.index.strftime("%Y-%m-%d").tolist()
    equity_values = equity_curve.tolist()

    pf_str  = f"{is_stats['pf']:.2f}"           if is_stats["pf"]           is not None else "N/A"
    wft_str = f"{wft_result['sharpe']:.2f}"     if wft_result               is not None else "N/A"
    print(
        f"{tag} 完了 — シャープ(IS)={is_stats['sharpe']:.2f}"
        f"  PF={pf_str}  WFT={wft_str}  取引={is_stats['n_trades']}回"
    )

    return {
        "symbol":        symbol,
        "params_dict":   params_dict,
        "is_stats":      is_stats,
        "wft_result":    wft_result,
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

_param_change_log: list = []

def check_param_change(symbol, new_params, prev_params):
    if symbol not in prev_params:
        return new_params

    prev     = prev_params[symbol]
    adjusted = dict(new_params)

    for key, limit in PARAM_LIMITS.items():
        if key not in prev or key not in adjusted:
            continue
        prev_val = prev[key]
        new_val  = adjusted[key]
        if prev_val == 0:
            continue
        change_rate = abs(new_val - prev_val) / abs(prev_val)
        if change_rate > limit:
            adjusted[key] = prev_val
            _param_change_log.append(f"  {key}: {prev_val} → {new_val} (変化率{change_rate*100:.1f}% > {limit*100:.0f}%) → 前回値を維持")

    return adjusted

# ==================== メイン ====================

if __name__ == "__main__":
    prev_params = load_prev_params()
    wft_cutoff  = END_DATE - relativedelta(months=WF_TEST_MONTHS)

    print(f"バックテスト期間 : {START_DATE.date()} 〜 {END_DATE.date()}")
    print(f"最適化 / WFT     : 〜 {wft_cutoff.date()} / {wft_cutoff.date()} 〜 {END_DATE.date()}")
    print(f"フォワードテスト : {FW_START_DATE.date()} 〜 {FW_END_DATE.date()}")
    print()

    raw_results = {}
    errors      = {}
    total       = len(SYMBOLS)

    for idx, symbol in enumerate(SYMBOLS, 1):
        print(f"\n{'='*50}")
        try:
            raw_results[symbol] = optimize_symbol(symbol, idx, total, wft_cutoff, prev_params)
        except Exception as e:
            errors[symbol] = str(e)
            print(f"[{idx}/{total}] {symbol} エラー: {e}")

    fw_results = {}
    print(f"\n{'='*50}")
    print("フォワードテスト開始")
    for idx, symbol in enumerate(raw_results, 1):
        print(f"[{idx}/{len(raw_results)}] {symbol} フォワードテスト中...")
        fw_results[symbol] = run_forward_test(symbol, raw_results[symbol]["params_dict"])
        fw = fw_results[symbol]
        if fw:
            pf_str = f"{fw['pf']:.2f}" if fw["pf"] is not None else "N/A"
            print(
                f"  完了 — シャープ={fw['sharpe']:.2f}  PF={pf_str}"
                f"  最大DD={fw['max_dd']:.1f}%  勝率={fw['win_rate']:.1f}%  取引={fw['n_trades']}回"
            )
        else:
            print("  データ不足のためスキップ")

    if errors:
        print()
        for sym, msg in errors.items():
            print(f"  ⚠️ {sym} エラー: {msg}")

    # === 結果集計 ===
    results      = []
    equity_finals = []

    for symbol in SYMBOLS:
        if symbol not in raw_results:
            continue
        r    = raw_results[symbol]
        is_s = r["is_stats"]
        wft_r = r["wft_result"]
        pf_str  = f"{is_s['pf']:.2f}"       if is_s["pf"]  is not None else "N/A"
        wft_str = f"{wft_r['sharpe']:.2f}"  if wft_r       is not None else "N/A"
        equity_finals.append(is_s["equity_final"])
        results.append({
            "銘柄":         symbol,
            "最終資産":     f"¥{is_s['equity_final']:,.0f}",
            "総リターン":   f"{is_s['return_pct']:.1f}%",
            "年率リターン": f"{is_s['return_ann']:.1f}%",
            "最大DD":       f"{is_s['max_dd']:.1f}%",
            "勝率":         f"{is_s['win_rate']:.1f}%",
            "取引回数":     is_s["n_trades"],
            "PF":           pf_str,
            "シャープ(IS)": f"{is_s['sharpe']:.2f}",
            "シャープ(WFT)": wft_str,
            "_is_stats":    is_s,
            "_wft_result":  wft_r,
        })

    # === 結果表示 ===
    print("\n=== 全銘柄最適化バックテスト結果 ===")
    print(f"初期資金（各銘柄）: ¥{INITIAL_CASH:,}")
    print()
    display_cols = ["銘柄", "最終資産", "総リターン", "年率リターン", "最大DD", "勝率", "取引回数", "PF", "シャープ(IS)", "シャープ(WFT)"]
    df = pd.DataFrame(results)[display_cols]
    print(df.to_string(index=False))

    if equity_finals:
        total_final   = sum(equity_finals)
        total_initial = INITIAL_CASH * len(equity_finals)
        print(f"\n=== 合計 ===")
        print(f"総投資額: ¥{total_initial:,}")
        print(f"最終資産合計: ¥{total_final:,.0f}")
        print(f"総合リターン: {(total_final - total_initial) / total_initial * 100:.1f}%")

    print("\n=== 銘柄別最適パラメータ一覧 ===")
    for symbol in SYMBOLS:
        if symbol in raw_results:
            print(f"{symbol}: {raw_results[symbol]['params_dict']}")

    # === 除外判定・params.json 保存 ===
    results_dict = {r["銘柄"]: r for r in results}
    params_out   = {}
    excluded     = []

    for symbol in SYMBOLS:
        if symbol not in raw_results:
            continue
        r     = results_dict[symbol]
        is_s  = r["_is_stats"]
        wft_r = r["_wft_result"]
        new_p = raw_results[symbol]["params_dict"]

        # 除外条件の強化
        wft_sharpe = wft_r["sharpe"] if wft_r else None
        pf         = is_s["pf"]
        n_trades   = is_s["n_trades"]

        if wft_sharpe is not None and wft_sharpe < 0:
            reason = f"WFTシャープ:{wft_sharpe:.2f}"
            excluded.append(f"{symbol}({reason})")
            continue

        if pf is not None and pf < PF_THRESHOLD:
            reason = f"PF:{pf:.2f} < {PF_THRESHOLD}"
            excluded.append(f"{symbol}({reason})")
            continue

        if n_trades < MIN_TRADES:
            reason = f"取引回数:{n_trades}回 < {MIN_TRADES}回"
            excluded.append(f"{symbol}({reason})")
            continue

        if is_s["sharpe"] >= SHARPE_THRESHOLD:
            adjusted_p = check_param_change(symbol, new_p, prev_params)
            params_out[symbol] = adjusted_p
        else:
            reason = f"シャープ:{is_s['sharpe']:.2f}"
            excluded.append(f"{symbol}({reason})")

    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params":     params_out,
        "excluded":   excluded,
    }
    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    if excluded:
        print("\n=== 除外ペア ===")
        for e in excluded:
            print(f"  ⚠️ {e}")

    if _param_change_log:
        print("\n=== パラメータ急変チェック ===")
        for line in _param_change_log:
            print(f"  ⚠️ {line}")

    print(f"\nparams.json に保存しました。")

    # === backtest_results.json 保存 ===
    bt_results = {}
    for symbol in SYMBOLS:
        if symbol not in raw_results:
            continue
        r    = raw_results[symbol]
        is_s = r["is_stats"]
        wft_r = r["wft_result"]
        bt_results[symbol] = {
            "bt": {
                "sharpe":   is_s["sharpe"],
                "pf":       is_s["pf"],
                "max_dd":   is_s["max_dd"],
                "win_rate": is_s["win_rate"],
                "n_trades": is_s["n_trades"],
            },
            "wft": {
                "sharpe":   wft_r["sharpe"]   if wft_r else None,
                "pf":       wft_r["pf"]       if wft_r else None,
                "max_dd":   wft_r["max_dd"]   if wft_r else None,
                "win_rate": wft_r["win_rate"] if wft_r else None,
                "n_trades": wft_r["n_trades"] if wft_r else None,
            },
            "fw": fw_results.get(symbol),
            "dates":  r.get("equity_dates",  []),
            "equity": r.get("equity_values", []),
        }

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(bt_results, f, indent=2, ensure_ascii=False)
    print(f"backtest_results.json に保存しました。")
