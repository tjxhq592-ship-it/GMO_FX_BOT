"""
GPU加速バックテスト (CuPy RawKernel)

各CUDAスレッドが1パラメータ組み合わせの時系列全体を処理。
インジケーター（BB/RSI/ATR）はCPU側で事前計算してGPUへ転送。
シミュレーションループだけをGPU上で並列実行。

ImprovedStrategy と同一ロジック:
  - ロング: 終値 < BB下限 AND RSI <= rsi_lower
  - ショート: 終値 > BB上限 AND RSI >= rsi_upper
  - 決済: SL/TP ヒット or BB中心線クロス
"""

from __future__ import annotations

import math
import numpy as np
import pandas as pd
import cupy as cp
import warnings
from typing import Dict, List, Tuple

warnings.filterwarnings("ignore", category=UserWarning, module="cupy")

# ── CUDAカーネルソース ────────────────────────────────────────────────────────

_KERNEL_SRC = r"""
extern "C" __global__
void backtest_sim(
    const double* close,          /* (T,)               */
    const double* high,           /* (T,)               */
    const double* low,            /* (T,)               */
    const double* bb_upper_flat,  /* (n_bb * T,) C順    */
    const double* bb_mid_flat,    /* (n_bb * T,)        */
    const double* bb_lower_flat,  /* (n_bb * T,)        */
    const double* rsi_flat,       /* (n_rsi * T,)       */
    const double* atr_flat,       /* (n_atr * T,)       */
    const int*    bb_param_idx,   /* (N,)               */
    const int*    rsi_idx,        /* (N,)               */
    const int*    atr_idx,        /* (N,)               */
    const double* rsi_upper,      /* (N,)               */
    const double* rsi_lower,      /* (N,)               */
    const double* sl_mult,        /* (N,)               */
    const double* tp_mult,        /* (N,)               */
    int    N,
    int    T,
    double initial_cash,
    double trade_size,
    double commission,
    /* 出力 */
    double* out_equity,
    int*    out_n_trades,
    int*    out_n_wins,
    double* out_max_dd,
    double* out_gross_profit,
    double* out_gross_loss,
    double* out_sum_ret,
    double* out_sum_ret_sq,
    int*    out_n_ret
)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= N) return;

    int    bb_idx = bb_param_idx[idx];
    int    rsi_i  = rsi_idx[idx];
    int    atr_i  = atr_idx[idx];
    double rsi_u  = rsi_upper[idx];
    double rsi_l  = rsi_lower[idx];
    double sl_m   = sl_mult[idx];
    double tp_m   = tp_mult[idx];

    double equity      = initial_cash;
    double peak_equity = initial_cash;
    double prev_equity = initial_cash;
    int    position    = 0;   /* 0:フラット  1:ロング  -1:ショート */
    double entry_p     = 0.0;
    double sl_p        = 0.0;
    double tp_p        = 0.0;

    int    n_trades    = 0;
    int    n_wins      = 0;
    double max_dd_val  = 0.0;
    double gross_p     = 0.0;
    double gross_l     = 0.0;
    double sum_r       = 0.0;
    double sum_r_sq    = 0.0;
    int    n_ret       = 0;

    for (int t = 1; t < T; t++) {
        double price  = close[t];
        double high_t = high[t];
        double low_t  = low[t];
        double atr_t  = atr_flat[atr_i * T + t];
        double rsi_t  = rsi_flat[rsi_i * T + t];
        double bb_u   = bb_upper_flat[bb_idx * T + t];
        double bb_m   = bb_mid_flat  [bb_idx * T + t];
        double bb_l   = bb_lower_flat[bb_idx * T + t];

        /* NaN バー（ウォームアップ期間）はスキップ */
        if (atr_t != atr_t || rsi_t != rsi_t || bb_u != bb_u) goto update_curve;

        /* ── 決済ロジック ──────────────────────────────────────────── */
        if (position == 1) {
            double exit_p = 0.0;
            if      (low_t  <= sl_p)  exit_p = sl_p;
            else if (high_t >= tp_p)  exit_p = tp_p;
            else if (price  >  bb_m)  exit_p = price;

            if (exit_p > 0.0) {
                double ret = (exit_p - entry_p) / entry_p - 2.0 * commission;
                double pnl = equity * trade_size * ret;
                equity += pnl;
                n_trades++;
                if (pnl > 0.0) { gross_p += pnl; n_wins++; }
                else            { gross_l += (-pnl); }
                position = 0;
            }
        } else if (position == -1) {
            double exit_p = 0.0;
            if      (high_t >= sl_p)  exit_p = sl_p;
            else if (low_t  <= tp_p)  exit_p = tp_p;
            else if (price  <  bb_m)  exit_p = price;

            if (exit_p > 0.0) {
                double ret = (entry_p - exit_p) / entry_p - 2.0 * commission;
                double pnl = equity * trade_size * ret;
                equity += pnl;
                n_trades++;
                if (pnl > 0.0) { gross_p += pnl; n_wins++; }
                else            { gross_l += (-pnl); }
                position = 0;
            }
        }

        /* ── エントリーロジック ────────────────────────────────────── */
        if (position == 0) {
            if (price < bb_l && rsi_t <= rsi_l) {
                position = 1;
                entry_p  = price;
                sl_p     = price - atr_t * sl_m;
                tp_p     = price + atr_t * tp_m;
            } else if (price > bb_u && rsi_t >= rsi_u) {
                position = -1;
                entry_p  = price;
                sl_p     = price + atr_t * sl_m;
                tp_p     = price - atr_t * tp_m;
            }
        }

        update_curve:;
        /* ── エクイティカーブ追跡 ──────────────────────────────────── */
        if (equity > peak_equity) peak_equity = equity;
        double dd = (peak_equity - equity) / peak_equity * 100.0;
        if (dd > max_dd_val) max_dd_val = dd;

        double pr = (prev_equity > 0.0) ? prev_equity : 1.0;
        double bar_r = (equity - pr) / pr;
        sum_r    += bar_r;
        sum_r_sq += bar_r * bar_r;
        n_ret++;
        prev_equity = equity;
    }

    out_equity      [idx] = equity;
    out_n_trades    [idx] = n_trades;
    out_n_wins      [idx] = n_wins;
    out_max_dd      [idx] = max_dd_val;
    out_gross_profit[idx] = gross_p;
    out_gross_loss  [idx] = gross_l;
    out_sum_ret     [idx] = sum_r;
    out_sum_ret_sq  [idx] = sum_r_sq;
    out_n_ret       [idx] = n_ret;
}
"""

_compiled_kernel: cp.RawKernel | None = None
_KERNEL_VERSION = 2  # rsi/atr period 対応に伴うカーネル更新


def _get_kernel() -> cp.RawKernel:
    global _compiled_kernel
    if _compiled_kernel is None:
        _compiled_kernel = cp.RawKernel(_KERNEL_SRC, "backtest_sim")
    return _compiled_kernel


# ── インジケーター計算（CPU・utils.py と同一ロジック） ────────────────────────

def _compute_bb(close: np.ndarray, period: int, std_mult: float
                ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger Bands: utils.calculate_bollinger と同一（rolling mean/std ddof=1）"""
    s = pd.Series(close)
    mid = s.rolling(period).mean().values
    std = s.rolling(period).std().values  # ddof=1
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def _compute_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI: utils.calculate_rsi と同一（rolling mean, not EWM）"""
    s     = pd.Series(close)
    delta = s.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    return (100 - 100 / (1 + rs)).values


def _compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 period: int = 14) -> np.ndarray:
    """ATR: utils.calculate_atr と同一（rolling mean of TR）"""
    h = pd.Series(high)
    l = pd.Series(low)
    c = pd.Series(close)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean().values


# ── GPU バックテスト メイン関数 ────────────────────────────────────────────────

# FX 30分足の年間バー数（FX市場: 平日5日×24時間×2本/h）
_BARS_PER_YEAR = 252 * 24 * 2  # ≈ 12096


def gpu_batch_backtest(
    data: pd.DataFrame,
    params_list: List[Dict],
    initial_cash: float = 1_000_000.0,
    trade_size: float = 0.2,
    commission: float = 0.00002,
) -> List[Dict]:
    """
    GPU上で params_list の全コンボを同時バックテスト。

    Returns:
        各コンボの統計 dict リスト（`_extract_stats` 互換キー）。
        エラーコンボは空 dict ではなくゼロ統計を返す。
    """
    close = data["Close"].values.astype(np.float64)
    high  = data["High"].values.astype(np.float64)
    low   = data["Low"].values.astype(np.float64)
    T = len(close)
    N = len(params_list)

    if N == 0 or T < 2:
        return []

    # ── ユニークBBパラメータを収集 ────────────────────────────────────────
    seen: dict[tuple, int] = {}
    bb_idx_list = []
    unique_bb: list[tuple] = []
    for p in params_list:
        key = (int(p["bb_period"]), float(p["bb_std"]))
        if key not in seen:
            seen[key] = len(unique_bb)
            unique_bb.append(key)
        bb_idx_list.append(seen[key])
    n_bb = len(unique_bb)

    # ── インジケーター事前計算（CPU） ─────────────────────────────────────
    bb_upper_all = np.empty((n_bb, T), dtype=np.float64)
    bb_mid_all   = np.empty((n_bb, T), dtype=np.float64)
    bb_lower_all = np.empty((n_bb, T), dtype=np.float64)

    for i, (period, std) in enumerate(unique_bb):
        u, m, l = _compute_bb(close, period, std)
        bb_upper_all[i] = u
        bb_mid_all[i]   = m
        bb_lower_all[i] = l

    # ── ユニーク RSI period を収集して事前計算 ──────────────────────────────
    seen_rsi: dict[int, int] = {}
    rsi_arrays: list[np.ndarray] = []
    for p in params_list:
        rp = int(p.get("rsi_period", 14))
        if rp not in seen_rsi:
            seen_rsi[rp] = len(rsi_arrays)
            rsi_arrays.append(_compute_rsi(close, period=rp))
    rsi_idx_list = [seen_rsi[int(p.get("rsi_period", 14))] for p in params_list]
    rsi_all = np.stack(rsi_arrays)  # (n_rsi, T)

    # ── ユニーク ATR period を収集して事前計算 ──────────────────────────────
    seen_atr: dict[int, int] = {}
    atr_arrays: list[np.ndarray] = []
    for p in params_list:
        ap = int(p.get("atr_period", 14))
        if ap not in seen_atr:
            seen_atr[ap] = len(atr_arrays)
            atr_arrays.append(_compute_atr(high, low, close, period=ap))
    atr_idx_list = [seen_atr[int(p.get("atr_period", 14))] for p in params_list]
    atr_all = np.stack(atr_arrays)  # (n_atr, T)

    # NaN → IEEE NaN のまま転送（カーネルで検出）
    # ── GPU 転送 ──────────────────────────────────────────────────────────
    close_g      = cp.asarray(close)
    high_g       = cp.asarray(high)
    low_g        = cp.asarray(low)
    bb_upper_g   = cp.asarray(bb_upper_all)   # (n_bb, T) C順
    bb_mid_g     = cp.asarray(bb_mid_all)
    bb_lower_g   = cp.asarray(bb_lower_all)
    rsi_flat_g   = cp.asarray(rsi_all.ravel())
    atr_flat_g   = cp.asarray(atr_all.ravel())
    bb_idx_g     = cp.asarray(np.array(bb_idx_list, dtype=np.int32))
    rsi_idx_g    = cp.asarray(np.array(rsi_idx_list, dtype=np.int32))
    atr_idx_g    = cp.asarray(np.array(atr_idx_list, dtype=np.int32))
    rsi_upper_g  = cp.asarray(np.array([p["rsi_upper"]   for p in params_list], dtype=np.float64))
    rsi_lower_g  = cp.asarray(np.array([p["rsi_lower"]   for p in params_list], dtype=np.float64))
    sl_mult_g    = cp.asarray(np.array([p["atr_sl_mult"] for p in params_list], dtype=np.float64))
    tp_mult_g    = cp.asarray(np.array([p["atr_tp_mult"] for p in params_list], dtype=np.float64))

    # ── 出力バッファ ──────────────────────────────────────────────────────
    out_equity      = cp.zeros(N, dtype=cp.float64)
    out_n_trades    = cp.zeros(N, dtype=cp.int32)
    out_n_wins      = cp.zeros(N, dtype=cp.int32)
    out_max_dd      = cp.zeros(N, dtype=cp.float64)
    out_gross_profit= cp.zeros(N, dtype=cp.float64)
    out_gross_loss  = cp.zeros(N, dtype=cp.float64)
    out_sum_ret     = cp.zeros(N, dtype=cp.float64)
    out_sum_ret_sq  = cp.zeros(N, dtype=cp.float64)
    out_n_ret       = cp.zeros(N, dtype=cp.int32)

    # ── カーネル起動 ──────────────────────────────────────────────────────
    threads = 256
    blocks  = math.ceil(N / threads)
    kernel  = _get_kernel()
    kernel(
        (blocks,), (threads,),
        (
            close_g, high_g, low_g,
            bb_upper_g, bb_mid_g, bb_lower_g,
            rsi_flat_g, atr_flat_g,
            bb_idx_g, rsi_idx_g, atr_idx_g,
            rsi_upper_g, rsi_lower_g, sl_mult_g, tp_mult_g,
            np.int32(N), np.int32(T),
            np.float64(initial_cash), np.float64(trade_size),
            np.float64(commission),
            out_equity, out_n_trades, out_n_wins,
            out_max_dd, out_gross_profit, out_gross_loss,
            out_sum_ret, out_sum_ret_sq, out_n_ret,
        )
    )
    cp.cuda.Stream.null.synchronize()

    # ── CPU 転送・統計計算 ────────────────────────────────────────────────
    equity_np   = cp.asnumpy(out_equity)
    n_trades_np = cp.asnumpy(out_n_trades)
    n_wins_np   = cp.asnumpy(out_n_wins)
    max_dd_np   = cp.asnumpy(out_max_dd)
    gp_np       = cp.asnumpy(out_gross_profit)
    gl_np       = cp.asnumpy(out_gross_loss)
    sum_r_np    = cp.asnumpy(out_sum_ret)
    sum_r_sq_np = cp.asnumpy(out_sum_ret_sq)
    n_ret_np    = cp.asnumpy(out_n_ret)

    results: list[dict] = []
    for i in range(N):
        n      = int(n_trades_np[i])
        eq     = float(equity_np[i])
        nr     = int(n_ret_np[i])
        gp     = float(gp_np[i])
        gl     = float(gl_np[i])

        # Profit Factor
        if gl > 0.0:
            pf: float | None = gp / gl
        elif gp > 0.0:
            pf = None   # ∞ → None（_extract_stats と統一）
        else:
            pf = None

        # リターン %
        return_pct = (eq - initial_cash) / initial_cash * 100.0

        # シャープ比（バーリターンの平均/標準偏差 × √年間バー数）
        if nr > 1:
            mean_r  = sum_r_np[i] / nr
            var_r   = max(0.0, sum_r_sq_np[i] / nr - mean_r * mean_r)
            std_r   = math.sqrt(var_r)
            sharpe  = (mean_r / std_r * math.sqrt(_BARS_PER_YEAR)) if std_r > 1e-12 else 0.0
        else:
            sharpe = 0.0

        win_rate = (float(n_wins_np[i]) / n * 100.0) if n > 0 else 0.0

        results.append({
            "sharpe":       sharpe,
            "pf":           pf,
            "max_dd":       float(max_dd_np[i]),
            "win_rate":     win_rate,
            "n_trades":     n,
            "return_pct":   return_pct,
            "equity_final": eq,
        })

    return results


def is_gpu_available() -> bool:
    """CuPyとGPUが利用可能か確認"""
    try:
        import cupy as cp
        cp.cuda.runtime.getDeviceCount()
        return True
    except Exception:
        return False
