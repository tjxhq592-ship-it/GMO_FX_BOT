# -*- coding: utf-8 -*-
import os
import sys
import matplotlib
matplotlib.use("Agg")

from backtesting import Backtest, Strategy
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import hashlib
import logging
import warnings
import pandas as pd
import pickle
import json
import time
from utils import (
    calculate_rsi as _calculate_rsi,
    calculate_atr as _calculate_atr,
    calculate_bollinger as _calculate_bollinger,
)

# Windows の DETACHED_PROCESS 起動では sys.stdout/stderr が None になる場合がある。
# 書き込みテストで有効性を確認し、失敗なら devnull へリダイレクト。
def _ensure_valid_stream(stream_name: str) -> None:
    stream = getattr(sys, stream_name, None)
    try:
        if stream is None:
            raise AttributeError("None")
        stream.write("")
        stream.flush()
    except Exception:
        devnull = open(os.devnull, "w", encoding="utf-8", errors="replace")
        setattr(sys, stream_name, devnull)

_ensure_valid_stream("stdout")
_ensure_valid_stream("stderr")

# ログ・警告の抑制
logging.getLogger("peewee").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

# backtesting.py が numpy 配列を期待するためラップ
def calculate_rsi(prices, period=14):
    result = _calculate_rsi(pd.Series(prices), period=period)
    return result.values if hasattr(result, "values") else result

def calculate_atr(high, low, close, period=14):
    result = _calculate_atr(pd.Series(high), pd.Series(low), pd.Series(close), period=period)
    return result.values if hasattr(result, "values") else result

def _bb_upper(close, period, std_mult):
    bb = _calculate_bollinger(pd.Series(close), period=period, std_mult=std_mult)
    return bb["upper"].values

def _bb_mid(close, period, std_mult):
    bb = _calculate_bollinger(pd.Series(close), period=period, std_mult=std_mult)
    return bb["mid"].values

def _bb_lower(close, period, std_mult):
    bb = _calculate_bollinger(pd.Series(close), period=period, std_mult=std_mult)
    return bb["lower"].values

# === backtest_config.json から設定を読み込む ===
CONFIG_FILE = "backtest_config.json"

def _load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"{CONFIG_FILE} が見つかりません。")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

_cfg = _load_config()

# 日付
_start_str = _cfg.get("start_date", "auto")
END_DATE   = datetime.now() - timedelta(days=1)   # end_date="auto"
if _start_str == "auto":
    START_DATE = END_DATE - relativedelta(years=2)
else:
    START_DATE = datetime.strptime(_start_str, "%Y-%m-%d")

FW_START_DATE = datetime.now() - timedelta(days=90)
FW_END_DATE   = datetime.now() - timedelta(days=1)

# WF 期間
WF_TRAIN_MONTHS   = int(_cfg.get("wf_train_months",   12))
WF_TEST_MONTHS    = int(_cfg.get("wf_test_months",     1))
# WFT 期間を直近からずらすオフセット（月数）
# 例: wft_offset_months=2, wf_test_months=2 → WFT = END-4ヶ月 〜 END-2ヶ月
WFT_OFFSET_MONTHS = int(_cfg.get("wft_offset_months",  0))

# 除外条件
MIN_TRADES       = int(_cfg.get("min_trades",     200))
PF_THRESHOLD     = float(_cfg.get("min_pf",       1.2))
SHARPE_THRESHOLD = float(_cfg.get("min_wft_sharpe", 0.0))

INITIAL_CASH = 1_000_000  # 円（固定）
CACHE_DIR    = ".cache"
CACHE_TTL    = 4 * 3600   # 4時間（4時間足のため）
PARAMS_FILE  = "params.json"
RESULTS_FILE = "backtest_results.json"

# ── ペア別スプレッド設定（GMOクリック証券FXネオ 原則固定スプレッド） ──────
# 円ペア  : 銭単位（例: 0.2 = 0.2銭）
# クロスペア: 0.1pips単位の整数（例: 1 = 0.1pips, 15 = 1.5pips）
SPREAD_PIPS = {
    # 円ペア（銭単位）
    "USD_JPY":  0.2,    # 0.2銭
    "EUR_JPY":  0.4,    # 0.4銭
    "GBP_JPY":  0.7,    # 0.7銭
    "AUD_JPY":  0.4,    # 0.4銭
    "NZD_JPY":  0.7,    # 0.7銭
    "CAD_JPY":  0.4,    # 0.4銭
    "CHF_JPY":  0.8,    # 0.8銭
    "ZAR_JPY":  9.0,    # 9.0銭
    "TRY_JPY": 14.0,    # 14.0銭
    "MXN_JPY":  4.0,    # 4.0銭
    # クロスペア（0.1pips単位の整数）
    "EUR_USD":  1,      # 0.1pips
    "GBP_USD":  5,      # 0.5pips
    "AUD_USD":  4,      # 0.4pips
    "NZD_USD":  7,      # 0.7pips
    "EUR_GBP":  5,      # 0.5pips
    "EUR_AUD": 15,      # 1.5pips
    "EUR_CHF":  7,      # 0.7pips
    "GBP_CHF": 12,      # 1.2pips
    "GBP_AUD": 12,      # 1.2pips
    "AUD_NZD": 10,      # 1.0pips
    "USD_CHF":  9,      # 0.9pips
}

def calc_commission(symbol: str, price: float) -> float:
    """API手数料（固定） + スプレッド（価格比率）を合算して返す。

    円ペア  : spread は銭単位 → 円換算（÷100）→ price で割って率に変換
              例) USD_JPY 0.2銭 → 0.2/100/150 ≒ 0.0000133
    クロスペア: spread は 0.1pips単位の整数 → ×0.00001 → price で割って率に変換
              例) EUR_AUD 15(=1.5pips) → 15*0.00001/1.63 ≒ 0.0000920
    """
    spread = SPREAD_PIPS.get(symbol, 10)  # デフォルト10（=1.0pips）
    if price <= 0:
        return 0.00002
    if symbol.endswith("_JPY"):
        # 銭単位 → 円（÷100）→ レートで割って率に変換
        spread_rate = (spread / 100) / price
    else:
        # 0.1pips単位の整数 → ×0.00001 → price で割って率に変換
        spread_rate = (spread * 0.00001) / price
    return 0.00002 + spread_rate

# 対象シンボル: active_symbols（グリッドサーチ採用済みでトレード対象）
SYMBOLS = _cfg.get("active_symbols", _cfg.get("symbols", ["AUD_NZD"]))

# === GMO FX クライアント ===
from config import GMO_API_KEY, GMO_SECRET_KEY
from gmo_client import GmoFxClient
_gmo_client = GmoFxClient(GMO_API_KEY, GMO_SECRET_KEY)

# === データキャッシュ ===
def _cache_path(symbol: str) -> str:
    """シンボル単位のキャッシュパス（日付なし・TTL で鮮度管理）"""
    key = f"gmo_{symbol}_4hour"
    return os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".pkl")

def _download_gmo(symbol: str) -> pd.DataFrame:
    """GMO KLine API から直近2年分の4時間足を取得して返す"""
    df = _gmo_client.get_klines_bulk(symbol, interval="4hour", years=2)
    if df.empty:
        raise ValueError(f"{symbol}: GMO APIからデータ取得失敗")
    # backtesting ライブラリ用に列名を大文字化
    df.columns = [c.capitalize() for c in df.columns]
    # タイムゾーン除去
    if df.index.tz is not None:
        df.index = df.index.tz_convert(None)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

def get_historical_data(symbol: str) -> pd.DataFrame:
    """バックテスト用データを取得（TTLキャッシュ付き）"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        with open(path, "rb") as f:
            return pickle.load(f)
    df = _download_gmo(symbol)
    with open(path, "wb") as f:
        pickle.dump(df, f)
    return df

def get_forward_data(symbol: str) -> pd.DataFrame:
    """フォワードテスト期間（直近90日）のデータを取得"""
    df = get_historical_data(symbol)
    return df[df.index >= pd.Timestamp(FW_START_DATE)]

# === 戦略: ボリンジャーバンド + RSI 逆張り ===
class ImprovedStrategy(Strategy):
    bb_period   = 20
    bb_std      = 2.0
    rsi_period  = 14
    rsi_upper   = 70
    rsi_lower   = 30
    atr_period  = 14
    atr_sl_mult = 1.5
    atr_tp_mult = 2.0
    trade_size  = 0.2

    def init(self):
        close = self.data.Close
        high  = self.data.High
        low   = self.data.Low

        self.bb_upper = self.I(_bb_upper, close, self.bb_period, self.bb_std)
        self.bb_mid   = self.I(_bb_mid,   close, self.bb_period, self.bb_std)
        self.bb_lower = self.I(_bb_lower, close, self.bb_period, self.bb_std)
        self.rsi      = self.I(calculate_rsi, close, self.rsi_period)
        self.atr      = self.I(calculate_atr, high, low, close, self.atr_period)

    def next(self):
        price = self.data.Close[-1]
        atr   = self.atr[-1]

        long_sl  = price - atr * self.atr_sl_mult
        long_tp  = price + atr * self.atr_tp_mult
        short_sl = price + atr * self.atr_sl_mult
        short_tp = price - atr * self.atr_tp_mult

        # ── ロングエントリー: 終値がBB下限割れ + RSI売られすぎ ─────────
        if price < self.bb_lower[-1] and self.rsi[-1] <= self.rsi_lower:
            if self.position.is_short:
                self.position.close()
            if not self.position:
                self.buy(size=self.trade_size, sl=long_sl, tp=long_tp)

        # ── ショートエントリー: 終値がBB上限超え + RSI買われすぎ ────────
        elif price > self.bb_upper[-1] and self.rsi[-1] >= self.rsi_upper:
            if self.position.is_long:
                self.position.close()
            if not self.position:
                self.sell(size=self.trade_size, sl=short_sl, tp=short_tp)

        # ── ロング決済: 終値がBB中心線を上回った ───────────────────────
        elif self.position.is_long and price > self.bb_mid[-1]:
            self.position.close()

        # ── ショート決済: 終値がBB中心線を下回った ─────────────────────
        elif self.position.is_short and price < self.bb_mid[-1]:
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
def walk_forward_test(data, params_dict, symbol: str = ""):
    # WFT 期間: END_DATE から wft_offset_months 遡った時点を終端とする
    # 例: offset=2, test=2 → END-4ヶ月 〜 END-2ヶ月
    wft_end   = END_DATE - relativedelta(months=WFT_OFFSET_MONTHS)
    wft_start = wft_end  - relativedelta(months=WF_TEST_MONTHS)
    test_data = data[(data.index >= wft_start) & (data.index < wft_end)]
    if len(test_data) < 20:
        return None
    _price = float(test_data["Close"].iloc[-1])
    bt = Backtest(test_data, ImprovedStrategy, cash=INITIAL_CASH,
                  commission=calc_commission(symbol, _price))
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
    _price = float(fw_data["Close"].iloc[-1])
    bt = Backtest(fw_data, ImprovedStrategy, cash=INITIAL_CASH,
                  commission=calc_commission(symbol, _price))
    stats = bt.run(**params_dict)
    return _extract_stats(stats)

# === 1銘柄分のバックテスト（params.json の保存済みパラメータを使用） ===
def optimize_symbol(symbol, idx, total, wft_cutoff, saved_params):
    """
    最適化は行わず、saved_params（params.json）に保存済みのパラメータで
    Backtest.run() を1回だけ実行する。
    """
    _ensure_valid_stream("stdout")
    _ensure_valid_stream("stderr")
    tag = f"[{idx}/{total}] {symbol}"

    if symbol not in saved_params:
        raise ValueError(f"{symbol} のパラメータが params.json に存在しません")

    params_dict = saved_params[symbol]

    print(f"{tag} データ取得中...")
    data = get_historical_data(symbol)
    print(f"  データ: {len(data)}件 ({data.index[0]} 〜 {data.index[-1]})")

    # IS バックテスト（全期間データで run）
    print(f"{tag} バックテスト実行中...")
    _price = float(data["Close"].iloc[-1])
    bt_full = Backtest(data, ImprovedStrategy, cash=INITIAL_CASH,
                       commission=calc_commission(symbol, _price))
    stats_full = bt_full.run(**params_dict)
    is_stats = _extract_stats(stats_full)

    # エクイティカーブ
    equity_curve  = stats_full["_equity_curve"]["Equity"]
    equity_dates  = equity_curve.index.strftime("%Y-%m-%d").tolist()
    equity_values = equity_curve.tolist()

    # WFT（直近 WF_TEST_MONTHS だけで run）
    print(f"{tag} WFTテスト中...")
    wft_result = walk_forward_test(data, params_dict, symbol=symbol)

    pf_str  = f"{is_stats['pf']:.2f}"       if is_stats["pf"] is not None else "N/A"
    wft_str = f"{wft_result['sharpe']:.2f}" if wft_result is not None     else "N/A"
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

GRID_PROGRESS_FILE   = "grid_search_progress.json"
GRID_SEARCH_CFG_FILE = "grid_search_config.json"
BT_PROGRESS_FILE     = "backtest_progress.json"

_bt_log_lines: list = []

def _bt_log(msg: str) -> None:
    """print しつつ backtest_progress.json 用のログバッファに追記。
    デタッチドプロセス等で sys.stdout が None の場合は print をスキップ。"""
    try:
        if sys.stdout is not None:
            print(msg, flush=True)
    except Exception:
        pass
    _bt_log_lines.append(msg)

def _write_bt_progress(current: int, total: int, symbol: str, status: str) -> None:
    data = {
        "current":        current,
        "total_symbols":  total,
        "current_symbol": symbol,
        "status":         status,
        "log":            _bt_log_lines[-50:],
    }
    with open(BT_PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# === グリッドサーチジョブ ===
def grid_search_job(config: dict, score_weights: dict) -> list:
    """
    backtest_config.json の範囲から全パラメータ組み合わせを生成し、
    各組み合わせでバックテストを実行してスコアリングする。
    進捗は grid_search_progress.json にリアルタイムで書き出す。
    """
    import itertools

    symbols = config.get("symbols", SYMBOLS)

    bb_cfg   = config.get("bb_period", {"min": 10, "max": 30, "step": 5})
    bb_stds  = config.get("bb_std",    [1.0, 1.5, 2.0, 2.5])
    ru_cfg   = config.get("rsi_upper", {"min": 60, "max": 75, "step": 5})
    rl_cfg   = config.get("rsi_lower", {"min": 25, "max": 40, "step": 5})
    sl_mults = config.get("atr_sl_mult", [1.5, 2.0])
    tp_mults = config.get("atr_tp_mult", [2.0, 2.5])

    bb_periods  = list(range(bb_cfg["min"], bb_cfg["max"] + 1, bb_cfg.get("step", 5)))
    rsi_uppers  = list(range(ru_cfg["min"], ru_cfg["max"] + 1, ru_cfg.get("step", 5)))
    rsi_lowers  = list(range(rl_cfg["min"], rl_cfg["max"] + 1, rl_cfg.get("step", 5)))

    combos = list(itertools.product(
        bb_periods, bb_stds, rsi_uppers, rsi_lowers, sl_mults, tp_mults
    ))
    total   = len(combos) * len(symbols)
    current = 0
    start_t = time.time()
    results = []
    best_score = 0.0
    best_params: dict = {}

    wt_wft    = score_weights.get("wft_sharpe", 0.4)
    wt_is     = score_weights.get("is_sharpe",  0.2)
    wt_pf     = score_weights.get("pf",         0.2)
    wt_trades = score_weights.get("trades",      0.2)

    wft_cutoff = END_DATE - relativedelta(months=WF_TEST_MONTHS)

    for symbol in symbols:
        try:
            data       = get_historical_data(symbol)
            train_data = data[data.index < wft_cutoff]
        except Exception as e:
            print(f"  {symbol} データ取得失敗: {e}")
            current += len(combos)
            continue

        for (bb_p, bb_s, rsi_u, rsi_l, sl_m, tp_m) in combos:
            current += 1
            params_dict = {
                "bb_period":   bb_p,
                "bb_std":      bb_s,
                "rsi_period":  14,
                "rsi_upper":   rsi_u,
                "rsi_lower":   rsi_l,
                "atr_period":  14,
                "atr_sl_mult": sl_m,
                "atr_tp_mult": tp_m,
            }

            # IS バックテスト
            try:
                _price = float(train_data["Close"].iloc[-1])
                bt  = Backtest(train_data, ImprovedStrategy,
                               cash=INITIAL_CASH,
                               commission=calc_commission(symbol, _price))
                st_is = bt.run(**params_dict)
                is_s  = _extract_stats(st_is)
            except Exception:
                is_s = None

            # WFT
            wft_r = walk_forward_test(data, params_dict, symbol=symbol) if is_s else None

            # スコアリング
            score = 0.0
            if is_s:
                n    = is_s["n_trades"]
                pf   = is_s["pf"] or 0.0
                is_sharpe  = is_s["sharpe"]
                wft_sharpe = wft_r["sharpe"] if wft_r else float("nan")

                if n >= 50 and not (wft_sharpe != wft_sharpe) and wft_sharpe >= 0:
                    score = (
                        wft_sharpe              * wt_wft +
                        max(is_sharpe, 0.0)     * wt_is  +
                        max(pf,        0.0)     * wt_pf  +
                        min(n / 200.0, 1.0)     * wt_trades
                    )

            row = {
                "symbol":      symbol,
                "bb_period":   bb_p,
                "bb_std":      bb_s,
                "rsi_upper":   rsi_u,
                "rsi_lower":   rsi_l,
                "atr_sl_mult": sl_m,
                "atr_tp_mult": tp_m,
                "n_trades":    is_s["n_trades"]  if is_s else 0,
                "pf":          is_s["pf"]        if is_s else None,
                "is_sharpe":   is_s["sharpe"]    if is_s else None,
                "wft_sharpe":  wft_r["sharpe"]   if wft_r else None,
                "score":       round(score, 4),
            }
            results.append(row)

            if score > best_score:
                best_score  = score
                best_params = {**params_dict, "symbol": symbol}

            # 進捗書き出し
            elapsed = int(time.time() - start_t)
            remaining = int(elapsed / current * (total - current)) if current else 0
            progress = {
                "current":     current,
                "total":       total,
                "best_score":  round(best_score, 4),
                "best_params": best_params,
                "elapsed":     elapsed,
                "remaining":   remaining,
            }
            with open(GRID_PROGRESS_FILE, "w", encoding="utf-8") as _f:
                json.dump(progress, _f, ensure_ascii=False, indent=2)

    # スコア降順ソート
    results.sort(key=lambda x: x["score"], reverse=True)

    # 最終進捗（完了）
    elapsed = int(time.time() - start_t)
    with open(GRID_PROGRESS_FILE, "w", encoding="utf-8") as _f:
        json.dump({
            "current": total, "total": total,
            "best_score": round(best_score, 4),
            "best_params": best_params,
            "elapsed": elapsed, "remaining": 0,
            "done": True,
        }, _f, ensure_ascii=False, indent=2)

    # 結果を JSON 保存
    with open("grid_search_results.json", "w", encoding="utf-8") as _f:
        json.dump(results[:50], _f, indent=2, ensure_ascii=False)

    print(f"\nグリッドサーチ完了: {total}件  ベストスコア={best_score:.4f}")
    print(f"ベストパラメータ: {best_params}")
    return results


def run_backtest_job() -> None:
    """scheduler.py から毎週月曜に呼び出すエントリーポイント"""
    import sys
    print("=== 週次バックテスト開始 ===")
    # __main__ ブロックと同じ処理を関数化して呼び出す
    # （グローバル変数に依存しているため sys.argv をダミー指定して再実行）
    import subprocess
    result = subprocess.run(
        [sys.executable, __file__],
        capture_output=False,
    )
    if result.returncode != 0:
        print("バックテストジョブがエラーで終了しました")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid-search", action="store_true",
                        help="グリッドサーチモードで実行")
    args = parser.parse_args()

    if args.grid_search:
        # grid_search_config.json からスコア重みを読み込む
        if os.path.exists(GRID_SEARCH_CFG_FILE):
            with open(GRID_SEARCH_CFG_FILE, "r", encoding="utf-8") as f:
                gs_cfg = json.load(f)
            # "score_weights" または "weights" どちらのキーにも対応
            score_weights = gs_cfg.get("score_weights") or gs_cfg.get("weights", {})
        else:
            score_weights = {
                "wft_sharpe": 0.4, "is_sharpe": 0.2,
                "pf": 0.2, "trades": 0.2,
            }
        grid_search_job(_cfg, score_weights)
    else:
        # ── 通常バックテスト（params.json の保存済みパラメータで run のみ実行） ──
        wft_cutoff  = END_DATE - relativedelta(months=WF_TEST_MONTHS)

        # params.json から保存済みパラメータを読み込む
        try:
            with open(PARAMS_FILE, "r", encoding="utf-8") as _f:
                saved_params = json.load(_f).get("params", {})
        except Exception:
            saved_params = {}

        if not saved_params:
            _bt_log("params.json にパラメータがありません。グリッドサーチを先に実行してください。")
            _write_bt_progress(0, 0, "", "error")
            raise SystemExit(1)

        # active_symbols との積集合をバックテスト対象とする
        TARGET_SYMBOLS = [s for s in SYMBOLS if s in saved_params] or list(saved_params.keys())

        _bt_log(f"バックテスト期間 : {START_DATE.date()} 〜 {END_DATE.date()}")
        _bt_log(f"WFT cutoff       : {wft_cutoff.date()}")
        _bt_log(f"フォワードテスト : {FW_START_DATE.date()} 〜 {FW_END_DATE.date()}")
        _bt_log(f"対象ペア         : {TARGET_SYMBOLS}")
        _bt_log("（最適化なし: params.json のパラメータで直接 run）")
        _bt_log("")

        raw_results = {}
        errors      = {}
        total       = len(TARGET_SYMBOLS)
        _write_bt_progress(0, total, "", "running")

        for idx, symbol in enumerate(TARGET_SYMBOLS, 1):
            _bt_log(f"\n{'='*50}")
            _bt_log(f"[{idx}/{total}] {symbol} 開始...")
            _write_bt_progress(idx - 1, total, symbol, "running")
            _ensure_valid_stream("stdout")
            _ensure_valid_stream("stderr")
            try:
                raw_results[symbol] = optimize_symbol(symbol, idx, total, wft_cutoff, saved_params)
                _bt_log(f"[{idx}/{total}] {symbol} 完了")
            except Exception as e:
                errors[symbol] = str(e)
                _bt_log(f"[{idx}/{total}] {symbol} エラー: {e}")
            _write_bt_progress(idx, total, symbol, "running")

        fw_results = {}
        _bt_log(f"\n{'='*50}")
        _bt_log("フォワードテスト開始")
        for idx, symbol in enumerate(raw_results, 1):
            _bt_log(f"[{idx}/{len(raw_results)}] {symbol} フォワードテスト中...")
            fw_results[symbol] = run_forward_test(symbol, raw_results[symbol]["params_dict"])
            fw = fw_results[symbol]
            if fw:
                pf_str = f"{fw['pf']:.2f}" if fw["pf"] is not None else "N/A"
                _bt_log(
                    f"  完了 — シャープ={fw['sharpe']:.2f}  PF={pf_str}"
                    f"  最大DD={fw['max_dd']:.1f}%  勝率={fw['win_rate']:.1f}%  取引={fw['n_trades']}回"
                )
            else:
                _bt_log("  データ不足のためスキップ")

        if errors:
            _bt_log("")
            for sym, msg in errors.items():
                _bt_log(f"  ⚠️ {sym} エラー: {msg}")

        # === 結果集計 ===
        if not raw_results:
            _bt_log("\n全銘柄でエラーが発生しました。処理を終了します。")
            _write_bt_progress(total, total, "", "error")
            raise SystemExit(1)

        results       = []
        equity_finals = []

        for symbol in TARGET_SYMBOLS:
            if symbol not in raw_results:
                continue
            r     = raw_results[symbol]
            is_s  = r["is_stats"]
            wft_r = r["wft_result"]
            pf_str  = f"{is_s['pf']:.2f}"      if is_s["pf"] is not None else "N/A"
            wft_str = f"{wft_r['sharpe']:.2f}"  if wft_r      is not None else "N/A"
            equity_finals.append(is_s["equity_final"])
            results.append({
                "銘柄":          symbol,
                "最終資産":      f"¥{is_s['equity_final']:,.0f}",
                "総リターン":    f"{is_s['return_pct']:.1f}%",
                "年率リターン":  f"{is_s['return_ann']:.1f}%",
                "最大DD":        f"{is_s['max_dd']:.1f}%",
                "勝率":          f"{is_s['win_rate']:.1f}%",
                "取引回数":      is_s["n_trades"],
                "PF":            pf_str,
                "シャープ(IS)":  f"{is_s['sharpe']:.2f}",
                "シャープ(WFT)": wft_str,
                "_is_stats":     is_s,
                "_wft_result":   wft_r,
            })

        # === 結果表示 ===
        print("\n=== 全銘柄バックテスト結果 ===")
        print(f"初期資金（各銘柄）: ¥{INITIAL_CASH:,}")
        print()
        display_cols = ["銘柄", "最終資産", "総リターン", "年率リターン", "最大DD", "勝率",
                        "取引回数", "PF", "シャープ(IS)", "シャープ(WFT)"]
        df = pd.DataFrame(results)[display_cols]
        print(df.to_string(index=False))

        if equity_finals:
            total_final   = sum(equity_finals)
            total_initial = INITIAL_CASH * len(equity_finals)
            print(f"\n=== 合計 ===")
            print(f"総投資額: ¥{total_initial:,}")
            print(f"最終資産合計: ¥{total_final:,.0f}")
            print(f"総合リターン: {(total_final - total_initial) / total_initial * 100:.1f}%")

        print("\n=== 銘柄別パラメータ一覧 ===")
        for symbol in TARGET_SYMBOLS:
            if symbol in raw_results:
                print(f"{symbol}: {raw_results[symbol]['params_dict']}")

        # params.json は変更しない（グリッドサーチが管理するため）

        # === backtest_results.json 保存 ===
        bt_results = {}
        for symbol in TARGET_SYMBOLS:
            if symbol not in raw_results:
                continue
            r     = raw_results[symbol]
            is_s  = r["is_stats"]
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
                    "pf":       wft_r["pf"]        if wft_r else None,
                    "max_dd":   wft_r["max_dd"]    if wft_r else None,
                    "win_rate": wft_r["win_rate"]  if wft_r else None,
                    "n_trades": wft_r["n_trades"]  if wft_r else None,
                },
                "fw":     fw_results.get(symbol),
                "dates":  r.get("equity_dates",  []),
                "equity": r.get("equity_values", []),
            }

        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(bt_results, f, indent=2, ensure_ascii=False)
        _bt_log(f"backtest_results.json に保存しました。")
        _write_bt_progress(total, total, "", "completed")
