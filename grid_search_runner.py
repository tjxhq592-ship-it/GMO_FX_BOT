"""
グリッドサーチ独立実行スクリプト
起動コマンド:
  python grid_search_runner.py            # 通常実行（並列）
  python grid_search_runner.py --debug    # 最初の1パターンのみテスト実行
  python grid_search_runner.py --diagnose # EUR_GBP 先頭10パターン詳細診断

プランB: 1コア1銘柄の multiprocessing.Process 直接並列化（ProcessPoolExecutor廃止）。
各ワーカーは担当銘柄の全コンボをシングルスレッドで処理し、
500コンボごとに {PROGRESS_FILE}.{symbol} へ進捗を書き出す。
メインプロセスはバックグラウンドスレッドで各シンボルの進捗ファイルを
3秒ごとにポーリングし、ダッシュボード用の grid_search_progress.json を更新する。
"""
import json
import multiprocessing
import os
import subprocess
import sys
import time
import traceback
import itertools
import threading
# ProcessPoolExecutor は multiprocessing.Process に移行したため不使用
# from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from dateutil.relativedelta import relativedelta

import psutil

# Windows の DETACHED_PROCESS 起動では sys.stdout/stderr が None または
# 無効なハンドルになる。書き込みテストで有効性を確認し、失敗なら devnull へ。
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

# backtest.py の関数・定数をインポート
from backtest import (
    _cfg,
    SYMBOLS, START_DATE, END_DATE, FW_START_DATE, FW_END_DATE,
    WF_TEST_MONTHS, WFT_OFFSET_MONTHS, INITIAL_CASH,
    SPREAD_PIPS, calc_commission,
    get_historical_data,
    walk_forward_test,
    _extract_stats,
    ImprovedStrategy,
)
from backtesting import Backtest

PID_FILE      = "grid_search_pid.json"
PROGRESS_FILE = "grid_search_progress.json"
RESULTS_FILE  = "grid_search_results.json"
PARAMS_FILE   = "params.json"
GS_CFG_FILE   = "grid_search_config.json"
BT_CFG_FILE   = "backtest_config.json"

# 進捗ファイル書き込みの排他制御
_progress_lock = threading.Lock()


# ── 動的ワーカー数決定 ──────────────────────────────────────────────────────

def get_optimal_workers(symbols: list, max_from_cfg: int = 14) -> int:
    """CPU使用率・メモリ使用率・設定値に基づいて最適なワーカー数を返す"""
    cpu_count = os.cpu_count() or 4
    cpu_usage = psutil.cpu_percent(interval=1)
    mem_usage = psutil.virtual_memory().percent

    base = min(len(symbols), max_from_cfg, max(1, cpu_count - 2))
    if cpu_usage > 80:
        base = max(1, base // 2)
    if mem_usage > 85:
        base = max(1, base // 2)
    return max(1, base)


# ── 1銘柄のグリッドサーチ（multiprocessing.Process のターゲット関数） ────────

def search_one_symbol(symbol, combos, wt_wft, wt_is, wt_pf, wt_trades,
                      wft_cutoff_ts, progress_file_base, result_queue,
                      progress_queue):
    """1銘柄の全コンボをシングルスレッドで処理し、結果を result_queue に put する。
    進捗は progress_queue 経由でメインプロセスに送り、メインプロセスが一元書き込みする。
    multiprocessing.Process(target=...) から別プロセスで実行される。
    """
    # Windows DETACHED_PROCESS 対応
    _ensure_valid_stream("stdout")
    _ensure_valid_stream("stderr")

    print(f"[{symbol}] 処理開始 コンボ数: {len(combos)}", flush=True)

    from datetime import datetime as _dt
    wft_cutoff = _dt.fromtimestamp(wft_cutoff_ts)

    # ── データ取得 ──────────────────────────────────────────────────────────
    try:
        sym_data       = get_historical_data(symbol)
        sym_train_data = sym_data[sym_data.index < wft_cutoff]
        print(f"[{symbol}] データ取得完了 {len(sym_data)}件", flush=True)
    except Exception as e:
        result_queue.put({
            "symbol":      symbol,
            "error":       str(e),
            "rows":        [],
            "best_score":  0.0,
            "best_params": {},
            "best_row":    None,
        })
        return

    rows:        list  = []
    best_score:  float = 0.0
    best_params: dict  = {}
    best_row:    dict | None = None
    total_combos = len(combos)

    # ── 全コンボを順次実行 ──────────────────────────────────────────────────
    for combo_i, (bb_p, bb_s, rsi_u, rsi_l, sl_m, tp_m) in enumerate(combos):
        params_dict = {
            "bb_period":   bb_p,  "bb_std":      bb_s,
            "rsi_period":  14,    "rsi_upper":   rsi_u,
            "rsi_lower":   rsi_l, "atr_period":  14,
            "atr_sl_mult": sl_m,  "atr_tp_mult": tp_m,
        }
        task_args = (combo_i, params_dict, wt_wft, wt_is, wt_pf, wt_trades,
                     symbol, sym_data, sym_train_data)
        try:
            _, params_dict, is_s, wft_r, score, err = _worker_task(task_args)
        except Exception:
            continue

        row = {
            "symbol":      symbol,
            "bb_period":   params_dict.get("bb_period"),
            "bb_std":      params_dict.get("bb_std"),
            "rsi_upper":   params_dict.get("rsi_upper"),
            "rsi_lower":   params_dict.get("rsi_lower"),
            "atr_sl_mult": params_dict.get("atr_sl_mult"),
            "atr_tp_mult": params_dict.get("atr_tp_mult"),
            "n_trades":    is_s["n_trades"] if is_s else 0,
            "pf":          is_s["pf"]       if is_s else None,
            "is_sharpe":   is_s["sharpe"]   if is_s else None,
            "wft_sharpe":  wft_r["sharpe"]  if wft_r else None,
            "score":       round(score, 4),
        }
        rows.append(row)

        if score > best_score:
            best_score  = score
            best_params = params_dict
            best_row    = row

        # 500コンボごと（および最終コンボ）に progress_queue で進捗を送信
        if combo_i % 500 == 0 or combo_i == total_combos - 1:
            try:
                progress_queue.put((symbol, combo_i + 1, total_combos,
                                    round(best_score, 4)))
            except Exception:
                pass

    result_queue.put({
        "symbol":      symbol,
        "error":       None,
        "rows":        rows,
        "best_score":  best_score,
        "best_params": best_params,
        "best_row":    best_row,
    })


def _worker_task(args: tuple) -> tuple:
    """スレッドワーカーで実行する1コンボのバックテスト。
    スレッドはメモリを共有するため data/train_data を直接引数で受け取る。
    numpy/pandas の演算は GIL を解放するためスレッド並列でも効果あり。
    """
    combo_idx, params_dict, wt_wft, wt_is, wt_pf, wt_trades, symbol, data, train_data = args

    is_s: dict | None = None
    err_msg: str | None = None

    # IS バックテスト
    try:
        _price = float(train_data["Close"].iloc[-1])
        bt    = Backtest(train_data, ImprovedStrategy,
                         cash=INITIAL_CASH, commission=calc_commission(symbol, _price))
        st_is = bt.run(**params_dict)
        is_s  = _extract_stats(st_is)
    except Exception as e:
        err_msg = f"IS 例外: {type(e).__name__}: {e}"
        return combo_idx, params_dict, None, None, 0.0, err_msg

    # WFT
    wft_r: dict | None = None
    try:
        wft_r = walk_forward_test(data, params_dict, symbol=symbol)
    except Exception as e:
        err_msg = f"WFT 例外: {type(e).__name__}: {e}"
        wft_r = None

    # スコアリング（除外条件は後段の _save_to_params で判定するためここでは NaN チェックのみ）
    score = 0.0
    if is_s:
        n          = is_s["n_trades"]
        pf         = is_s["pf"] or 0.0
        is_sharpe  = is_s["sharpe"]
        wft_sharpe = wft_r["sharpe"] if wft_r else float("nan")
        nan_check  = wft_sharpe != wft_sharpe

        if n >= 50 and not nan_check:
            score = (
                wft_sharpe           * wt_wft +
                max(is_sharpe, 0.0)  * wt_is  +
                max(pf, 0.0)         * wt_pf  +
                min(n / 200.0, 1.0)  * wt_trades
            )

    return combo_idx, params_dict, is_s, wft_r, round(score, 4), err_msg


# ── シンボルレベルシングルスレッド用ワーカー（--diagnose / 旧互換用） ────
# _run_symbol_search はスレッドから直接呼ばれることもある（診断モード等）。

def _run_symbol_search(args: tuple) -> dict:
    """1シンボル分のグリッドサーチを実行してベストスコアを返す"""
    (symbol, combos, wt_wft, wt_is, wt_pf, wt_trades,
     wft_cutoff_ts, min_trades, min_pf, min_wft_sharpe) = args

    _ensure_valid_stream("stdout")
    _ensure_valid_stream("stderr")

    from datetime import datetime as _dt
    wft_cutoff = _dt.fromtimestamp(wft_cutoff_ts)

    # データ取得
    try:
        data       = get_historical_data(symbol)
        train_data = data[data.index < wft_cutoff]
    except Exception as e:
        return {"symbol": symbol, "error": str(e), "rows": [],
                "best_score": 0.0, "best_params": {}, "best_row": None}

    rows:            list = []
    sym_best_score:  float = 0.0
    sym_best_params: dict  = {}
    sym_best_row:    dict | None = None

    for (bb_p, bb_s, rsi_u, rsi_l, sl_m, tp_m) in combos:
        params_dict = {
            "bb_period":   bb_p,  "bb_std":      bb_s,
            "rsi_period":  14,    "rsi_upper":   rsi_u,
            "rsi_lower":   rsi_l, "atr_period":  14,
            "atr_sl_mult": sl_m,  "atr_tp_mult": tp_m,
        }

        # IS バックテスト
        try:
            _price = float(train_data["Close"].iloc[-1])
            bt    = Backtest(train_data, ImprovedStrategy,
                             cash=INITIAL_CASH,
                             commission=calc_commission(symbol, _price))
            st_is = bt.run(**params_dict)
            is_s  = _extract_stats(st_is)
        except Exception:
            continue

        # WFT
        try:
            wft_r = walk_forward_test(data, params_dict, symbol=symbol)
        except Exception:
            wft_r = None

        # スコアリング（除外条件は後段で判定するため NaN チェックのみ）
        score = 0.0
        if is_s:
            n          = is_s["n_trades"]
            pf         = is_s["pf"] or 0.0
            is_sharpe  = is_s["sharpe"]
            wft_sharpe = wft_r["sharpe"] if wft_r else float("nan")
            nan_check  = wft_sharpe != wft_sharpe
            if n >= 50 and not nan_check:
                score = (
                    wft_sharpe           * wt_wft +
                    max(is_sharpe, 0.0)  * wt_is  +
                    max(pf, 0.0)         * wt_pf  +
                    min(n / 200.0, 1.0)  * wt_trades
                )

        row = {
            "symbol":      symbol,
            "bb_period":   bb_p,   "bb_std":      bb_s,
            "rsi_upper":   rsi_u,  "rsi_lower":   rsi_l,
            "atr_sl_mult": sl_m,   "atr_tp_mult": tp_m,
            "n_trades":    is_s["n_trades"] if is_s else 0,
            "pf":          is_s["pf"]       if is_s else None,
            "is_sharpe":   is_s["sharpe"]   if is_s else None,
            "wft_sharpe":  wft_r["sharpe"]  if wft_r else None,
            "score":       round(score, 4),
        }
        rows.append(row)

        if score > sym_best_score:
            sym_best_score  = score
            sym_best_params = params_dict
            sym_best_row    = row

    return {
        "symbol":      symbol,
        "error":       None,
        "rows":        rows,
        "best_score":  sym_best_score,
        "best_params": sym_best_params,
        "best_row":    sym_best_row,
    }


# ── ヘルパー ──────────────────────────────────────────────────────────────

def _write_pid(status: str) -> None:
    data = {
        "pid":        os.getpid(),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status":     status,
    }
    with open(PID_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _update_pid_status(status: str) -> None:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["status"] = status
        with open(PID_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _write_progress(current, total, best_score, best_params,
                    elapsed, remaining, log_lines, done=False,
                    completed_symbols: dict | None = None,
                    current_symbol: str = "",
                    symbol_current: int = 0,
                    symbol_total: int = 0,
                    ranking: list | None = None,
                    symbol_progress: dict | None = None) -> None:
    """
    symbol_progress: 並列実行中の各銘柄のリアルタイム進捗
      {"USD_JPY": {"current": 500, "total": 9600, "pct": 5.2, "best_score": 0.0}, ...}
    """
    data = {
        "current":           current,
        "total":             total,
        "best_score":        round(best_score, 4),
        "best_params":       best_params,
        "elapsed":           elapsed,
        "remaining":         remaining,
        "status":            "completed" if done else "running",
        "log":               log_lines[-50:],
        "completed_symbols": completed_symbols or {},
        "current_symbol":    current_symbol,
        "symbol_current":    symbol_current,
        "symbol_total":      symbol_total,
        "ranking":           ranking or [],
        "symbol_progress":   symbol_progress or {},
    }
    with _progress_lock:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _update_active_symbols(symbol: str, adopt: bool, log_fn) -> None:
    """backtest_config.json の active_symbols を更新する。
    adopt=True で追加、False で除去。
    """
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), BT_CFG_FILE)
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = {}
        active = cfg.get("active_symbols", cfg.get("symbols", []))
        if adopt:
            if symbol not in active:
                active.append(symbol)
        else:
            active = [s for s in active if s != symbol]
        cfg["active_symbols"] = active
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log_fn(f"  [WARN] active_symbols 更新失敗: {e}")


def _save_to_params(symbol: str, params_dict: dict | None, log_fn,
                    exclude_reason: str | None = None) -> None:
    """params.json と backtest_config.json の active_symbols を更新する。
    params_dict が None（除外）: params[symbol] を削除、active_symbols から除去。
    採用: params[symbol] を保存、active_symbols に追加。
    """
    try:
        if os.path.exists(PARAMS_FILE):
            with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                params_data = json.load(f)
        else:
            params_data = {"params": {}, "excluded": []}

        params_data.setdefault("params", {})
        params_data.setdefault("excluded", [])

        if params_dict is not None:
            # 採用: params に保存、excluded から除去、active_symbols に追加
            best_params = {
                "bb_period":   params_dict.get("bb_period"),
                "bb_std":      params_dict.get("bb_std"),
                "rsi_period":  14,
                "rsi_upper":   params_dict.get("rsi_upper"),
                "rsi_lower":   params_dict.get("rsi_lower"),
                "atr_period":  14,
                "atr_sl_mult": params_dict.get("atr_sl_mult"),
                "atr_tp_mult": params_dict.get("atr_tp_mult"),
            }
            params_data["params"][symbol] = best_params
            params_data["excluded"] = [
                e for e in params_data["excluded"]
                if not (isinstance(e, str) and e.startswith(f"{symbol}("))
            ]
            _update_active_symbols(symbol, adopt=True, log_fn=log_fn)
        else:
            # 除外: params から削除、excluded に追加、active_symbols から除去
            params_data["params"].pop(symbol, None)
            params_data["excluded"] = [
                e for e in params_data["excluded"]
                if not (isinstance(e, str) and e.startswith(f"{symbol}("))
            ]
            entry = f"{symbol}({exclude_reason})" if exclude_reason else symbol
            params_data["excluded"].append(entry)
            _update_active_symbols(symbol, adopt=False, log_fn=log_fn)

        params_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(params_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log_fn(f"  [ERROR] params.json 保存失敗: {e}")


# ── シングルスレッド版（デバッグ・互換用） ──────────────────────────────

def _run_single(train_data, data, params_dict, wt_wft, wt_is, wt_pf, wt_trades,
                debug: bool = False, symbol: str = "") -> tuple:
    """1パラメータ組み合わせのバックテスト（IS + WFT）"""
    _ensure_valid_stream("stdout")
    _ensure_valid_stream("stderr")
    is_s: dict | None = None
    err_msg: str | None = None
    try:
        _price = float(train_data["Close"].iloc[-1])
        bt    = Backtest(train_data, ImprovedStrategy,
                         cash=INITIAL_CASH, commission=calc_commission(symbol, _price))
        st_is = bt.run(**params_dict)
        is_s  = _extract_stats(st_is)
        if debug:
            print(f"  IS 結果: 取引={is_s['n_trades']}  シャープ={is_s['sharpe']:.3f}"
                  f"  PF={is_s['pf']}  DD={is_s['max_dd']:.1f}%")
    except (AttributeError, IOError, OSError) as e:
        _ensure_valid_stream("stdout")
        _ensure_valid_stream("stderr")
        err_msg = f"IS ストリームエラー: {type(e).__name__}: {e}"
        if debug:
            print(f"  [WARN] {err_msg}")
        return None, None, 0.0, err_msg
    except Exception as e:
        err_msg = f"IS 例外: {type(e).__name__}: {e}"
        if debug:
            print(f"  [ERROR] {err_msg}")
            traceback.print_exc()
        return None, None, 0.0, err_msg

    wft_r: dict | None = None
    try:
        wft_r = walk_forward_test(data, params_dict, symbol=symbol)
        if debug:
            if wft_r:
                print(f"  WFT結果: シャープ={wft_r['sharpe']:.3f}  PF={wft_r['pf']}"
                      f"  取引={wft_r['n_trades']}")
            else:
                print("  WFT結果: データ不足でスキップ")
    except Exception as e:
        err_msg = f"WFT 例外: {type(e).__name__}: {e}"
        if debug:
            print(f"  [ERROR] {err_msg}")
        wft_r = None

    score = 0.0
    if is_s:
        n          = is_s["n_trades"]
        pf         = is_s["pf"] or 0.0
        is_sharpe  = is_s["sharpe"]
        wft_sharpe = wft_r["sharpe"] if wft_r else float("nan")
        nan_check  = wft_sharpe != wft_sharpe
        if n >= 50 and not nan_check:
            score = (
                wft_sharpe           * wt_wft +
                max(is_sharpe, 0.0)  * wt_is  +
                max(pf, 0.0)         * wt_pf  +
                min(n / 200.0, 1.0)  * wt_trades
            )
        elif debug:
            reasons = []
            if n < 50:    reasons.append(f"取引回数不足({n}<50)")
            if nan_check: reasons.append("WFTシャープNaN")
            print(f"  スコア0の理由: {', '.join(reasons)}")

    return is_s, wft_r, score, err_msg


# ── デバッグモード ────────────────────────────────────────────────────────

def debug_run(config: dict, score_weights: dict) -> None:
    """1パターンのみ実行してスコアゼロ原因を詳細表示"""
    print("=" * 60)
    print("=== デバッグモード: スコアゼロ原因調査 ===")
    print("=" * 60)

    # デバッグ用固定パラメータ（GBP_USDでスコアが出た値）
    params_dict = {
        "bb_period":   25,
        "bb_std":      2.0,
        "rsi_period":  14,
        "rsi_upper":   80,
        "rsi_lower":   20,
        "atr_period":  14,
        "atr_sl_mult": 2.5,
        "atr_tp_mult": 1.5,
    }

    symbol     = "GBP_USD"
    wft_cutoff = END_DATE - relativedelta(months=WF_TEST_MONTHS)

    # 除外条件（backtest_config.json から読み込み）
    min_trades     = int(config.get("min_trades",      30))
    min_pf         = float(config.get("min_pf",        1.2))
    min_wft_sharpe = float(config.get("min_wft_sharpe", 0.0))

    print(f"\n対象シンボル : {symbol}")
    print(f"テストパラメータ: {params_dict}")
    print(f"データ期間  : {START_DATE.date()} ~ {END_DATE.date()}")
    print(f"WFT cutoff  : {wft_cutoff.date()}")
    print(f"スコア重み  : {score_weights}")
    print(f"除外条件    : 取引>={min_trades}  PF>={min_pf}  WFTシャープ>={min_wft_sharpe}")
    print()

    # ── [1] データ取得 ──────────────────────────────────────────────────────
    print("[1] データ取得中...")
    try:
        data = get_historical_data(symbol)
        print(f"  完了: {len(data)}件  ({data.index[0]} ~ {data.index[-1]})")
    except Exception as e:
        print(f"  [FATAL] {e}")
        traceback.print_exc()
        return

    train_data = data[data.index < wft_cutoff]
    test_data  = data[data.index >= wft_cutoff]
    print(f"  学習データ (IS)  : {len(train_data)}件  ({train_data.index[0] if len(train_data) else 'N/A'} ~ {wft_cutoff.date()})")
    print(f"  テストデータ (WFT): {len(test_data)}件  ({wft_cutoff.date()} ~ {END_DATE.date()})")

    # ── [2] コスト情報 ──────────────────────────────────────────────────────
    _price      = float(data["Close"].iloc[-1])
    _spread     = SPREAD_PIPS.get(symbol, 0.0003)
    _commission = calc_commission(symbol, _price)
    unit        = "銭" if symbol.endswith("_JPY") else "pips"
    print(f"\n[2] コスト情報")
    print(f"  スプレッド: {_spread} ({symbol} / {unit})")
    print(f"  現在価格  : {_price}")
    print(f"  commission: {_commission:.8f}")
    print(f"  往復コスト: {_commission * 2 * 100:.6f}%")
    print(f"  1回の取引コスト（100万円）: {_commission * 2 * 1_000_000:.0f}円")

    wt_wft    = score_weights.get("wft_sharpe", 0.4)
    wt_is     = score_weights.get("is_sharpe",  0.2)
    wt_pf     = score_weights.get("pf",         0.2)
    wt_trades = score_weights.get("trades",      0.2)

    # ── [3] ISバックテスト ──────────────────────────────────────────────────
    print("\n[3] ISバックテスト実行中（学習データ）...")
    is_s, wft_r, score, err = _run_single(
        train_data, data, params_dict,
        wt_wft, wt_is, wt_pf, wt_trades, debug=True, symbol=symbol,
    )

    # ── [4] IS結果詳細 ──────────────────────────────────────────────────────
    print(f"\n[4] IS結果詳細")
    if is_s:
        n_trades  = is_s["n_trades"]
        win_rate  = is_s["win_rate"]
        pf        = is_s["pf"] if is_s["pf"] is not None else float("nan")
        is_sharpe = is_s["sharpe"]
        max_dd    = is_s["max_dd"]
        return_pct = is_s["return_pct"]
        print(f"  取引回数  : {n_trades}")
        print(f"  勝率      : {win_rate:.1f}%")
        print(f"  PF        : {pf:.4f}")
        print(f"  シャープ(IS): {is_sharpe:.4f}")
        print(f"  最大DD    : {max_dd:.4f}%")
        print(f"  総リターン: {return_pct:.4f}%")
    else:
        print("  IS結果なし（バックテスト失敗）")
        if err:
            print(f"  エラー: {err}")

    # ── [5] WFT結果詳細 ─────────────────────────────────────────────────────
    print(f"\n[5] WFT結果詳細")
    if wft_r:
        wft_sharpe = wft_r["sharpe"]
        wft_pf     = wft_r["pf"] if wft_r["pf"] is not None else float("nan")
        print(f"  取引回数  : {wft_r['n_trades']}")
        print(f"  勝率      : {wft_r['win_rate']:.1f}%")
        print(f"  PF        : {wft_pf:.4f}")
        print(f"  シャープ(WFT): {wft_sharpe:.4f}")
        print(f"  最大DD    : {wft_r['max_dd']:.4f}%")
        print(f"  総リターン: {wft_r['return_pct']:.4f}%")
    else:
        print("  WFT結果なし（データ不足またはエラー）")

    # ── [6] スコアゼロ判定 ──────────────────────────────────────────────────
    print(f"\n[6] スコアゼロ判定")
    score_zero_reasons = []

    if is_s is None:
        score_zero_reasons.append("ISバックテスト失敗")
    else:
        n_trades  = is_s["n_trades"]
        pf        = is_s["pf"] if is_s["pf"] is not None else 0.0
        is_sharpe = is_s["sharpe"]

        if n_trades < min_trades:
            reason = f"取引回数({n_trades}) < 最小値({min_trades})"
            score_zero_reasons.append(reason)
            print(f"  → スコア0: {reason}")

        if pf < min_pf:
            reason = f"PF({pf:.4f}) < 最小値({min_pf})"
            score_zero_reasons.append(reason)
            print(f"  → スコア0: {reason}")

        if wft_r is None:
            reason = "WFT結果なし（データ不足）"
            score_zero_reasons.append(reason)
            print(f"  → スコア0: {reason}")
        else:
            wft_sharpe = wft_r["sharpe"]
            nan_check  = wft_sharpe != wft_sharpe
            if nan_check:
                reason = "WFTシャープがNaN"
                score_zero_reasons.append(reason)
                print(f"  → スコア0: {reason}")
            elif wft_sharpe < min_wft_sharpe:
                reason = f"WFTシャープ({wft_sharpe:.4f}) < 最小値({min_wft_sharpe})"
                score_zero_reasons.append(reason)
                print(f"  → スコア0: {reason}")

    if not score_zero_reasons:
        print("  スコアゼロの条件に該当なし → スコア計算済み")

    # ── [6.5] スコア計算詳細 ────────────────────────────────────────────────
    if is_s and wft_r:
        _wft_sharpe = wft_r["sharpe"]
        _is_sharpe  = is_s["sharpe"]
        _pf         = is_s["pf"] or 0.0
        _n_trades   = is_s["n_trades"]
        _wt_wft    = wt_wft
        _wt_is     = wt_is
        _wt_pf     = wt_pf
        _wt_trades = wt_trades
        print(f"\n  スコア計算詳細:")
        print(f"    wft_sharpe={_wft_sharpe:.4f} × {_wt_wft} = {max(_wft_sharpe,0)*_wt_wft:.4f}")
        print(f"    is_sharpe ={_is_sharpe:.4f} × {_wt_is} = {max(_is_sharpe,0)*_wt_is:.4f}")
        print(f"    pf        ={_pf:.4f} × {_wt_pf} = {max(_pf,0)*_wt_pf:.4f}")
        print(f"    trades    ={_n_trades} × {_wt_trades} = {min(_n_trades/200,1)*_wt_trades:.4f}")
        _calc_score = (
            max(_wft_sharpe, 0) * _wt_wft +
            max(_is_sharpe,  0) * _wt_is  +
            max(_pf,         0) * _wt_pf  +
            min(_n_trades / 200.0, 1.0) * _wt_trades
        )
        print(f"    合計      ={_calc_score:.4f}  (ask_claude返却score={score:.4f})")
        if abs(_calc_score - score) > 1e-6:
            print(f"    ⚠️  計算値と返却値が不一致！スコアリング条件を再確認してください")

    # ── [7] 最終スコア ──────────────────────────────────────────────────────
    print(f"\n[7] 最終結果")
    print(f"  score: {score:.4f}")
    if err:
        print(f"  error: {err}")
    print("\n=== デバッグ完了 ===")


# ── メイン ────────────────────────────────────────────────────────────────

def main(debug: bool = False) -> None:
    if not debug:
        _write_pid("running")
    log_lines: list[str] = []

    def log(msg: str) -> None:
        try:
            if sys.stdout is not None:
                print(msg, flush=True)
        except Exception:
            pass
        log_lines.append(msg)

    try:
        # 起動直前に backtest_config.json を再読み込み（ダッシュボードでの変更を反映）
        _bt_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), BT_CFG_FILE)
        try:
            with open(_bt_cfg_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = _cfg  # fallback

        gs_cfg: dict = {}
        if os.path.exists(GS_CFG_FILE):
            with open(GS_CFG_FILE, "r", encoding="utf-8") as f:
                gs_cfg = json.load(f)
        score_weights = gs_cfg.get("score_weights") or gs_cfg.get("weights") or {"wft_sharpe": 0.4, "is_sharpe": 0.2, "pf": 0.2, "trades": 0.2}

        if debug:
            debug_run(config, score_weights)
            return

        wt_wft    = score_weights.get("wft_sharpe", 0.4)
        wt_is     = score_weights.get("is_sharpe",  0.2)
        wt_pf     = score_weights.get("pf",         0.2)
        wt_trades = score_weights.get("trades",      0.2)

        # grid_search_symbols を使用（未設定・空の場合はエラー）
        symbols = config.get("grid_search_symbols") or []
        if not symbols:
            log("エラー: グリッドサーチ対象ペアが設定されていません")
            log("ダッシュボードの設定タブでグリッドサーチ対象ペアを選択してください")
            _write_progress(0, 0, 0.0, {}, 0, 0,
                            ["エラー: グリッドサーチ対象ペアが未設定です。設定タブで選択してください。"])
            _update_pid_status("error")
            sys.exit(1)

        top_n   = int(config.get("grid_search_top_n", 3))

        bb_cfg   = config.get("bb_period", {"min": 10, "max": 30, "step": 5})
        bb_stds  = config.get("bb_std",    [1.0, 1.5, 2.0, 2.5])
        ru_cfg   = config.get("rsi_upper", {"min": 60, "max": 75, "step": 5})
        rl_cfg   = config.get("rsi_lower", {"min": 25, "max": 40, "step": 5})
        sl_mults = config.get("atr_sl_mult", [1.5, 2.0])
        tp_mults = config.get("atr_tp_mult", [2.0, 2.5])

        bb_periods = list(range(bb_cfg["min"], bb_cfg["max"] + 1, bb_cfg.get("step", 5)))
        rsi_uppers = list(range(ru_cfg["min"], ru_cfg["max"] + 1, ru_cfg.get("step", 5)))
        rsi_lowers = list(range(rl_cfg["min"], rl_cfg["max"] + 1, rl_cfg.get("step", 5)))

        combos = list(itertools.product(
            bb_periods, bb_stds, rsi_uppers, rsi_lowers, sl_mults, tp_mults
        ))
        total   = len(combos) * len(symbols)
        current = 0
        start_t = time.time()
        results = []
        best_score  = 0.0
        best_params: dict = {}

        # WFT 期間計算（オフセット対応）
        # 例: offset=2, test=2 → IS学習 = ~END-4ヶ月, WFT検証 = END-4ヶ月〜END-2ヶ月
        wft_offset_months = int(config.get("wft_offset_months", WFT_OFFSET_MONTHS))
        wft_end    = END_DATE - relativedelta(months=wft_offset_months)
        wft_cutoff = wft_end  - relativedelta(months=WF_TEST_MONTHS)

        min_trades     = int(config.get("min_trades",      100))
        min_pf         = float(config.get("min_pf",        1.2))
        min_wft_sharpe = float(config.get("min_wft_sharpe", 0.0))

        log(f"組み合わせ数: {len(combos)} x {len(symbols)}銘柄 = {total} 件")
        log(f"WFT期間: {wft_cutoff.date()} 〜 {wft_end.date()} (offset={wft_offset_months}ヶ月)")

        # 起動直後に progress ファイルを初期化（ダッシュボードが即座に「running」を検知できるよう）
        _write_progress(0, total, 0.0, {}, 0, 0,
                        [f"グリッドサーチ開始...", f"対象ペア ({len(symbols)}件): {symbols}"])
        log(f"スコア重み: WFT={wt_wft} IS={wt_is} PF={wt_pf} 取引={wt_trades}")
        log(f"除外条件: 取引>={min_trades}  PF>={min_pf}  WFTシャープ>={min_wft_sharpe}")
        log("")

        error_count       = 0
        completed_symbols: dict = {}

        # ── データ事前取得（キャッシュ温め・失敗検出） ───────────────────────
        log("全ペアのデータを事前取得中...")
        valid_symbols = []
        for symbol in symbols:
            try:
                data = get_historical_data(symbol)
                log(f"  {symbol}: {len(data)}件 OK")
                valid_symbols.append(symbol)
            except Exception as e:
                log(f"  {symbol}: 取得失敗 → スキップ ({e})")
                current += len(combos)
                completed_symbols[symbol] = {
                    "status": "error", "reason": f"データ取得失敗: {e}",
                    "best_score": 0.0, "best_params": {},
                }
        symbols = valid_symbols
        total   = len(combos) * len(symbols)  # 有効シンボルで再計算

        # データ取得完了を通知（ダッシュボードに「処理開始」を表示させる）
        log("全データ取得完了 処理開始...")
        _write_progress(0, total, 0.0, {}, int(time.time() - start_t), 0,
                        log_lines + ["全データ取得完了 処理開始..."])

        # ── 動的ワーカー数を決定 ─────────────────────────────────────────────
        max_workers_cfg = int(gs_cfg.get("max_workers", 4))
        max_workers     = get_optimal_workers(symbols, max_workers_cfg)
        log(f"=== プランB: 1コア1銘柄の並列グリッドサーチ ===")
        log(f"対象ペア ({len(symbols)}件): {symbols}")
        log(f"ワーカー数: {max_workers}  (CPU={os.cpu_count()} / "
            f"使用率={psutil.cpu_percent():.0f}% / MEM={psutil.virtual_memory().percent:.0f}%)")

        # ── progress_writer スレッド ────────────────────────────────────────
        # 各ワーカーが progress_queue に (symbol, current, total, best_score) を送信し、
        # メインプロセスのこのスレッドだけが grid_search_progress.json を書き込む。
        # ファイル競合を完全に排除し、ステータスを正確に管理する。
        _sym_prog_shared: dict = {
            s: {"current": 0, "total": len(combos), "status": "waiting", "best_score": 0.0}
            for s in symbols
        }
        _prog_writer_stop = threading.Event()

        def _progress_writer_thread(progress_queue):
            while not _prog_writer_stop.is_set():
                try:
                    msg = progress_queue.get(timeout=1)
                except Exception:
                    continue
                if msg == "DONE":
                    break
                sym, cur_c, tot_c, bsc = msg
                _sym_prog_shared[sym] = {
                    "current":    cur_c,
                    "total":      tot_c,
                    "status":     "running",
                    "best_score": bsc,
                    "pct":        round(cur_c / tot_c * 100, 1) if tot_c > 0 else 0,
                }
                elapsed = int(time.time() - start_t)
                # 全銘柄の進捗から完了コンボ数を推定
                done_combos = current + sum(
                    v.get("current", 0) for v in _sym_prog_shared.values()
                    if v.get("status") == "running"
                )
                done_ratio = done_combos / total if total > 0 else 1
                remaining  = int(elapsed / done_ratio * (1 - done_ratio)) if done_ratio > 0 else 0
                _write_progress(done_combos, total, best_score, best_params,
                                elapsed, remaining, log_lines,
                                completed_symbols=completed_symbols,
                                symbol_progress=dict(_sym_prog_shared))

        # Manager と2つのキューを一括作成
        manager        = multiprocessing.Manager()
        result_queue   = manager.Queue()
        progress_queue = manager.Queue()

        _pw_thread = threading.Thread(
            target=_progress_writer_thread, args=(progress_queue,), daemon=True
        )
        _pw_thread.start()

        # ── multiprocessing.Process で銘柄間を並列実行 ──────────────────────────
        # max_workers に従ってバッチ実行（一度に起動するプロセス数を制限）
        remaining_syms = list(symbols)
        completed_syms_count = 0

        try:
            while remaining_syms or True:
                # バッチ: max_workers 個ずつ起動
                batch = remaining_syms[:max_workers]
                remaining_syms = remaining_syms[max_workers:]

                if not batch:
                    break

                processes = []
                for sym in batch:
                    p = multiprocessing.Process(
                        target=search_one_symbol,
                        args=(sym, combos, wt_wft, wt_is, wt_pf, wt_trades,
                              wft_cutoff.timestamp(), PROGRESS_FILE,
                              result_queue, progress_queue),
                        name=f"gs-{sym}",
                        daemon=False,
                    )
                    p.start()
                    processes.append((sym, p))

                # 起動直後にプロセス生存確認
                time.sleep(1)
                for sym, p in processes:
                    log(f"プロセス {p.name}: alive={p.is_alive()} pid={p.pid}")

                # バッチ内の全プロセスが完了するまでポーリング
                alive = dict(processes)  # sym -> process
                while alive:
                    for sym, p in list(alive.items()):
                        if not p.is_alive():
                            p.join(timeout=10)
                            del alive[sym]

                    # キューから結果を取り出して即時処理
                    while not result_queue.empty():
                        try:
                            sym_result = result_queue.get_nowait()
                        except Exception:
                            break
                        symbol = sym_result.get("symbol", "")
                        completed_syms_count += 1

                        if sym_result.get("error"):
                            log(f"[{symbol}] エラー: {sym_result['error'].splitlines()[0]}")
                            completed_symbols[symbol] = {
                                "status": "error",
                                "reason": sym_result["error"].splitlines()[0],
                                "best_score": 0.0, "best_params": {},
                            }
                            current += len(combos)
                            error_count += 1
                        else:
                            sym_rows        = sym_result["rows"]
                            sym_best_score  = sym_result["best_score"]
                            sym_best_params = sym_result["best_params"]

                            results.extend(sym_rows)
                            current += len(combos)

                            if sym_best_score > best_score:
                                best_score  = sym_best_score
                                best_params = {**sym_best_params, "symbol": symbol}

                            log(f"[{symbol}] 完了 ({completed_syms_count}/{len(symbols)})  "
                                f"ベスト={sym_best_score:.4f}  コンボ={len(sym_rows)}/{len(combos)}")

                            completed_symbols[symbol] = {
                                "status":      "pending",
                                "reason":      "",
                                "best_score":  round(sym_best_score, 4),
                                "best_params": sym_best_params,
                            }
                            # 完了シンボルの symbol_progress ステータスを更新
                            _sym_prog_shared[symbol] = {
                                "current":    len(combos),
                                "total":      len(combos),
                                "status":     "completed",
                                "best_score": round(sym_best_score, 4),
                                "pct":        100.0,
                            }

                        elapsed    = int(time.time() - start_t)
                        done_ratio = completed_syms_count / len(symbols)
                        remaining  = int(elapsed / done_ratio * (1 - done_ratio)) if done_ratio > 0 else 0
                        _write_progress(current, total, best_score, best_params,
                                        elapsed, remaining, log_lines,
                                        completed_symbols=completed_symbols,
                                        current_symbol=symbol,
                                        symbol_current=len(combos),
                                        symbol_total=len(combos),
                                        symbol_progress=dict(_sym_prog_shared))

                    if alive:
                        time.sleep(2)

                # バッチ終了後にキューを再度ドレイン（取り漏れ防止）
                while not result_queue.empty():
                    try:
                        sym_result = result_queue.get_nowait()
                    except Exception:
                        break
                    symbol = sym_result.get("symbol", "")
                    if symbol and symbol not in completed_symbols:
                        completed_syms_count += 1
                        if sym_result.get("error"):
                            completed_symbols[symbol] = {
                                "status": "error",
                                "reason": sym_result["error"].splitlines()[0],
                                "best_score": 0.0, "best_params": {},
                            }
                            current += len(combos)
                            error_count += 1
                        else:
                            sym_rows        = sym_result["rows"]
                            sym_best_score  = sym_result["best_score"]
                            sym_best_params = sym_result["best_params"]
                            results.extend(sym_rows)
                            current += len(combos)
                            if sym_best_score > best_score:
                                best_score  = sym_best_score
                                best_params = {**sym_best_params, "symbol": symbol}
                            completed_symbols[symbol] = {
                                "status":      "pending",
                                "reason":      "",
                                "best_score":  round(sym_best_score, 4),
                                "best_params": sym_best_params,
                            }
                            _sym_prog_shared[symbol] = {
                                "current":    len(combos),
                                "total":      len(combos),
                                "status":     "completed",
                                "best_score": round(sym_best_score, 4),
                                "pct":        100.0,
                            }

        finally:
            _prog_writer_stop.set()
            try:
                progress_queue.put("DONE")
            except Exception:
                pass
            _pw_thread.join(timeout=5)
            try:
                manager.shutdown()
            except Exception:
                pass

        # ── 旧コードとの互換: シンボル完了情報を整形 ─────────────────────────
        # top_N 判定処理に渡すためにシンボルごとのベスト情報を取り出す
        # (除外判定ブロックは後続の ranked / adopted_set ロジックで実施)
        for symbol in list(completed_symbols.keys()):
            if completed_symbols[symbol]["status"] == "pending":
                sym_best_score = completed_symbols[symbol]["best_score"]
                exclude_reason: str | None = None
                sym_best_row = next(
                    (r for r in results
                     if r["symbol"] == symbol and r["score"] == sym_best_score),
                    None,
                )
                if sym_best_score <= 0.0 or sym_best_row is None:
                    exclude_reason = "全パターンでスコア0"
                elif sym_best_row.get("n_trades", 0) < min_trades:
                    exclude_reason = (
                        f"取引回数({sym_best_row['n_trades']}) < 最小値({min_trades})"
                    )
                elif (sym_best_row.get("pf") is not None
                      and sym_best_row["pf"] < min_pf):
                    exclude_reason = (
                        f"PF({sym_best_row['pf']:.2f}) < 最小値({min_pf})"
                    )
                elif (sym_best_row.get("wft_sharpe") is not None
                      and sym_best_row["wft_sharpe"] < min_wft_sharpe):
                    exclude_reason = (
                        f"WFTシャープ({sym_best_row['wft_sharpe']:.2f}) < 最小値({min_wft_sharpe})"
                    )
                # exclude_reason は後段 top_N ブロックで更新
                completed_symbols[symbol]["reason"] = exclude_reason or ""

        if error_count > 0:
            log(f"! エラー合計: {error_count} ペア")

        # ── 全ペア完了: top_N 採用判定 ─────────────────────────────────────

        # スコア降順でランキング（エラーペアは除く）
        ranked = sorted(
            [
                {"symbol": sym, "best_score": info["best_score"],
                 "best_params": info["best_params"], "reason": info["reason"]}
                for sym, info in completed_symbols.items()
                if info["status"] != "error"
            ],
            key=lambda x: x["best_score"],
            reverse=True,
        )
        # スコア0は採用対象外（有効なパラメータが見つからなかったペア）
        ranked_valid = [r for r in ranked if r["best_score"] > 0]
        adopted_set  = {r["symbol"] for r in ranked_valid[:top_n]}
        excluded_set = {r["symbol"] for r in ranked if r["symbol"] not in adopted_set}

        if not adopted_set:
            log("⚠️ 有効なパラメータが見つかりませんでした。探索範囲や除外条件を緩和してください。")

        log(f"\n=== top_N={top_n} 採用判定 ===")
        ranking_out: list[dict] = []
        for rank_i, r in enumerate(ranked, 1):
            sym   = r["symbol"]
            score_val = r["best_score"]
            adopt = sym in adopted_set
            status_str = "adopted" if adopt else "excluded"
            ranking_out.append({
                "rank": rank_i, "symbol": sym,
                "score": score_val, "status": status_str,
            })
            log(f"  #{rank_i} {sym}: score={score_val:.4f}  → {status_str}")

            if adopt:
                _save_to_params(sym, r["best_params"], log)
                log(f"  [ADOPTED] {sym} params.json に保存")
                completed_symbols[sym]["status"] = "saved"
            else:
                reason = r["reason"] or f"top_{top_n}圏外(score={score_val:.4f})"
                _save_to_params(sym, None, log, exclude_reason=reason)
                log(f"  [EXCLUDED] {sym}: {reason}")
                completed_symbols[sym]["status"]  = "excluded"
                completed_symbols[sym]["reason"]  = reason

        results.sort(key=lambda x: x["score"], reverse=True)

        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(results[:50], f, indent=2, ensure_ascii=False)

        elapsed = int(time.time() - start_t)
        log(f"\n=== 完了 ===  {total}件  ベストスコア={best_score:.4f}")
        log(f"結果を {RESULTS_FILE} に保存しました。")

        _write_progress(total, total, best_score, best_params, elapsed, 0, log_lines,
                        done=True, completed_symbols=completed_symbols,
                        ranking=ranking_out)
        _update_pid_status("completed")

    except Exception as e:
        tb = traceback.format_exc()
        log(f"予期しないエラー: {e}\n{tb}")
        _update_pid_status("error")
        _write_progress(0, 1, 0.0, {}, 0, 0,
                        log_lines + [f"FATAL: {e}", tb])
        sys.exit(1)


# ── 詳細診断モード ───────────────────────────────────────────────────────

def diagnose_run(config: dict, score_weights: dict) -> None:
    """EUR_GBP の最初10パターンをシングルスレッドで実行し、詳細診断を表示"""
    print("=" * 60)
    print("=== 詳細診断モード: EUR_GBP 最初10パターン（シングルスレッド） ===")
    print("=" * 60)

    symbol     = "EUR_GBP"
    wft_cutoff = END_DATE - relativedelta(months=WF_TEST_MONTHS)

    min_trades     = int(config.get("min_trades",      20))
    min_pf         = float(config.get("min_pf",        0.8))
    min_wft_sharpe = float(config.get("min_wft_sharpe", 0.5))

    wt_wft    = score_weights.get("wft_sharpe", 0.5)
    wt_is     = score_weights.get("is_sharpe",  0.15)
    wt_pf     = score_weights.get("pf",         0.2)
    wt_trades = score_weights.get("trades",      0.15)

    # ── パラメータ組み合わせ生成 ──────────────────────────────────────────
    bb_cfg   = config.get("bb_period", {"min": 10, "max": 35, "step": 5})
    bb_stds  = config.get("bb_std",    [1.0, 1.5, 2.0, 2.5, 3.0])
    ru_cfg   = config.get("rsi_upper", {"min": 65, "max": 80,  "step": 5})
    rl_cfg   = config.get("rsi_lower", {"min": 20, "max": 35,  "step": 5})
    sl_mults = config.get("atr_sl_mult", [1.0, 1.5, 2.0, 2.5])
    tp_mults = config.get("atr_tp_mult", [1.5, 2.0, 2.5, 3.0, 3.5])

    bb_periods = list(range(bb_cfg["min"], bb_cfg["max"] + 1, bb_cfg.get("step", 5)))
    rsi_uppers = list(range(ru_cfg["min"], ru_cfg["max"] + 1, ru_cfg.get("step", 5)))
    rsi_lowers = list(range(rl_cfg["min"], rl_cfg["max"] + 1, rl_cfg.get("step", 5)))

    all_combos = list(itertools.product(
        bb_periods, bb_stds, rsi_uppers, rsi_lowers, sl_mults, tp_mults
    ))
    total_combos = len(all_combos)

    print(f"\n対象シンボル : {symbol}")
    print(f"データ期間  : {START_DATE.date()} ~ {END_DATE.date()}")
    print(f"WFT cutoff  : {wft_cutoff.date()}")
    print(f"総組み合わせ: {total_combos} パターン（先頭10件のみ実行）")
    print(f"除外条件    : 取引>={min_trades}  PF>={min_pf}  WFTシャープ>={min_wft_sharpe}")
    print(f"スコア重み  : WFT={wt_wft}  IS={wt_is}  PF={wt_pf}  取引={wt_trades}")

    # ── データ取得 ────────────────────────────────────────────────────────
    print(f"\n[1] データ取得中...")
    try:
        data = get_historical_data(symbol)
        print(f"  完了: {len(data)}件  ({data.index[0]} ~ {data.index[-1]})")
    except Exception as e:
        print(f"  [FATAL] {e}")
        traceback.print_exc()
        return

    train_data = data[data.index < wft_cutoff]
    test_data  = data[data.index >= wft_cutoff]
    print(f"  学習(IS) : {len(train_data)}件  (~ {wft_cutoff.date()})")
    print(f"  検証(WFT): {len(test_data)}件   ({wft_cutoff.date()} ~)")

    # ── commission 表示 ───────────────────────────────────────────────────
    _price      = float(data["Close"].iloc[-1])
    _spread     = SPREAD_PIPS.get(symbol, 10)
    _commission = calc_commission(symbol, _price)
    print(f"\n[2] commission 情報")
    print(f"  スプレッド: {_spread} (0.1pips単位) = {_spread * 0.1:.1f}pips")
    print(f"  現在価格  : {_price:.5f}")
    print(f"  commission: {_commission:.8f}")
    print(f"  往復コスト: {_commission * 2 * 100:.6f}%")
    print(f"  1回取引コスト（100万円）: {_commission * 2 * 1_000_000:.0f}円")

    # ── 先頭10パターン実行 ────────────────────────────────────────────────
    target_combos = all_combos[:10]
    print(f"\n[3] 先頭10パターン実行（シングルスレッド）")
    print("-" * 60)

    nonzero_count = 0
    results = []

    for i, (bb_p, bb_s, rsi_u, rsi_l, sl_m, tp_m) in enumerate(target_combos, 1):
        params_dict = {
            "bb_period":   bb_p,  "bb_std":      bb_s,
            "rsi_period":  14,    "rsi_upper":   rsi_u,
            "rsi_lower":   rsi_l, "atr_period":  14,
            "atr_sl_mult": sl_m,  "atr_tp_mult": tp_m,
        }

        is_s, wft_r, score, err = _run_single(
            train_data, data, params_dict,
            wt_wft, wt_is, wt_pf, wt_trades,
            debug=False, symbol=symbol,
        )

        # 結果の取り出し
        n_trades   = is_s["n_trades"]  if is_s  else 0
        win_rate   = is_s["win_rate"]  if is_s  else float("nan")
        pf         = (is_s["pf"] if is_s["pf"] is not None else float("nan")) if is_s else float("nan")
        is_sharpe  = is_s["sharpe"]    if is_s  else float("nan")
        max_dd     = is_s["max_dd"]    if is_s  else float("nan")
        wft_sharpe = wft_r["sharpe"]   if wft_r else float("nan")
        wft_trades = wft_r["n_trades"] if wft_r else 0

        print(f"\nパターン{i:2d}: bb_period={bb_p} bb_std={bb_s} "
              f"rsi_u={rsi_u} rsi_l={rsi_l} sl={sl_m} tp={tp_m}")
        print(f"  IS  : 取引={n_trades:3d}  勝率={win_rate:5.1f}%  "
              f"PF={pf:7.4f}  シャープ={is_sharpe:7.4f}  DD={max_dd:6.2f}%")
        print(f"  WFT : 取引={wft_trades:3d}  シャープ={wft_sharpe:7.4f}")
        print(f"  score={score:.4f}", end="")

        # スコアゼロ判定
        zero_reasons = []
        if is_s is None:
            zero_reasons.append("ISバックテスト失敗")
        else:
            if n_trades < min_trades:
                zero_reasons.append(f"取引回数({n_trades})<{min_trades}")
            if not (pf != pf) and pf < min_pf:
                zero_reasons.append(f"PF({pf:.4f})<{min_pf}")
            if wft_r is None:
                zero_reasons.append("WFT結果なし")
            else:
                nan_wft = wft_sharpe != wft_sharpe
                if nan_wft:
                    zero_reasons.append("WFTシャープNaN")
                elif wft_sharpe < min_wft_sharpe:
                    zero_reasons.append(f"WFTシャープ({wft_sharpe:.4f})<{min_wft_sharpe}")
        if zero_reasons:
            print(f"  → スコア0理由: {' / '.join(zero_reasons)}")
        else:
            print()

        if err:
            print(f"  [ERR] {err}")

        if score > 0:
            nonzero_count += 1

        results.append({"params": params_dict, "score": score,
                        "n_trades": n_trades, "is_sharpe": is_sharpe,
                        "wft_sharpe": wft_sharpe, "pf": pf})

    # ── サマリー ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"[4] サマリー")
    print(f"  実行パターン数  : {len(target_combos)}")
    print(f"  スコア>0 のパターン: {nonzero_count} / {len(target_combos)} 件")
    if nonzero_count > 0:
        best = max(results, key=lambda r: r["score"])
        print(f"  ベストスコア    : {best['score']:.4f}")
        print(f"  ベストパラメータ: {best['params']}")
    print("=== 診断完了 ===")


# ── エントリーポイント ────────────────────────────────────────────────────

if __name__ == "__main__":
    # Windows の multiprocessing 対応（freeze_support は exe 化時に必要）
    from multiprocessing import freeze_support
    freeze_support()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true",
                        help="最初の1パターンのみ実行して詳細デバッグ情報を表示")
    parser.add_argument("--diagnose", action="store_true",
                        help="EUR_GBP 先頭10パターンをシングルスレッドで実行して詳細診断")
    args = parser.parse_args()

    if args.diagnose:
        # grid_search_config.json からスコア重みを読み込む
        _gs_cfg: dict = {}
        if os.path.exists(GS_CFG_FILE):
            with open(GS_CFG_FILE, "r", encoding="utf-8") as _f:
                _gs_cfg = json.load(_f)
        _score_weights = (_gs_cfg.get("score_weights") or _gs_cfg.get("weights")
                          or {"wft_sharpe": 0.5, "is_sharpe": 0.15, "pf": 0.2, "trades": 0.15})

        _bt_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), BT_CFG_FILE)
        with open(_bt_cfg_path, "r", encoding="utf-8") as _f:
            _diag_config = json.load(_f)

        diagnose_run(_diag_config, _score_weights)
    else:
        main(debug=args.debug)
