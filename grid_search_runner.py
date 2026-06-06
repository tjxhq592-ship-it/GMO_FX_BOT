"""
グリッドサーチ独立実行スクリプト
起動コマンド:
  python grid_search_runner.py            # 通常実行
  python grid_search_runner.py --debug    # 最初の1パターンのみテスト実行

dashboard.py とは完全に独立したプロセスとして動作する。
進捗は grid_search_progress.json にリアルタイムで書き出す。
"""
import json
import os
import sys
import time
import traceback
import itertools
from datetime import datetime
from dateutil.relativedelta import relativedelta

# Windows の DETACHED_PROCESS 起動では sys.stdout/stderr が None になる。
# print() が失敗しないよう、None の場合は devnull にリダイレクト。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

# backtest.py の関数・定数をインポート
from backtest import (
    _cfg,
    SYMBOLS, START_DATE, END_DATE, FW_START_DATE, FW_END_DATE,
    WF_TEST_MONTHS, INITIAL_CASH,
    get_historical_data,
    walk_forward_test,
    _extract_stats,
    ImprovedStrategy,
)
from backtesting import Backtest

PID_FILE      = "grid_search_pid.json"
PROGRESS_FILE = "grid_search_progress.json"
RESULTS_FILE  = "grid_search_results.json"
GS_CFG_FILE   = "grid_search_config.json"
BT_CFG_FILE   = "backtest_config.json"


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
                    elapsed, remaining, log_lines, done=False) -> None:
    data = {
        "current":     current,
        "total":       total,
        "best_score":  round(best_score, 4),
        "best_params": best_params,
        "elapsed":     elapsed,
        "remaining":   remaining,
        "status":      "completed" if done else "running",
        "log":         log_lines[-50:],
    }
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _run_single(train_data, data, params_dict, wt_wft, wt_is, wt_pf, wt_trades,
                debug: bool = False) -> tuple[dict | None, dict | None, float, str | None]:
    """
    1パラメータ組み合わせのバックテスト（IS + WFT）を実行してスコアを返す。
    戻り値: (is_stats, wft_result, score, error_message)
    """
    # IS バックテスト
    is_s: dict | None = None
    err_msg: str | None = None
    try:
        bt    = Backtest(train_data, ImprovedStrategy,
                         cash=INITIAL_CASH, commission=0.00002)
        st_is = bt.run(**params_dict)
        is_s  = _extract_stats(st_is)
        if debug:
            print(f"  IS 結果: 取引={is_s['n_trades']}  シャープ={is_s['sharpe']:.3f}"
                  f"  PF={is_s['pf']}  DD={is_s['max_dd']:.1f}%")
    except Exception as e:
        err_msg = f"IS バックテスト例外: {type(e).__name__}: {e}"
        if debug:
            print(f"  [ERROR] {err_msg}")
            traceback.print_exc()
        return None, None, 0.0, err_msg

    # WFT
    wft_r: dict | None = None
    try:
        wft_r = walk_forward_test(data, params_dict)
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
            traceback.print_exc()
        wft_r = None

    # スコアリング
    score = 0.0
    if is_s:
        n          = is_s["n_trades"]
        pf         = is_s["pf"] or 0.0
        is_sharpe  = is_s["sharpe"]
        wft_sharpe = wft_r["sharpe"] if wft_r else float("nan")
        nan_check  = wft_sharpe != wft_sharpe   # NaN 判定

        if n >= 50 and not nan_check and wft_sharpe >= 0:
            score = (
                wft_sharpe           * wt_wft +
                max(is_sharpe, 0.0)  * wt_is  +
                max(pf, 0.0)         * wt_pf  +
                min(n / 200.0, 1.0)  * wt_trades
            )
        elif debug:
            reasons = []
            if n < 50:
                reasons.append(f"取引回数不足({n}<50)")
            if nan_check:
                reasons.append("WFTシャープNaN")
            if not nan_check and wft_sharpe < 0:
                reasons.append(f"WFTシャープマイナス({wft_sharpe:.3f})")
            print(f"  スコア0の理由: {', '.join(reasons)}")

    return is_s, wft_r, score, err_msg


def debug_run(config: dict, score_weights: dict) -> None:
    """最初の1パターンのみ実行してデバッグ情報を詳細表示"""
    print("=" * 60)
    print("=== デバッグモード: 最初の1パターンのみ実行 ===")
    print("=" * 60)

    symbols = config.get("symbols", SYMBOLS)
    bb_cfg   = config.get("bb_period", {"min": 10, "max": 30, "step": 5})
    bb_stds  = config.get("bb_std",    [1.0, 1.5, 2.0, 2.5])
    ru_cfg   = config.get("rsi_upper", {"min": 60, "max": 75, "step": 5})
    rl_cfg   = config.get("rsi_lower", {"min": 25, "max": 40, "step": 5})
    sl_mults = config.get("atr_sl_mult", [1.5, 2.0])
    tp_mults = config.get("atr_tp_mult", [2.0, 2.5])

    bb_p  = bb_cfg["min"]
    bb_s  = bb_stds[0]
    rsi_u = ru_cfg["min"]
    rsi_l = rl_cfg["min"]
    sl_m  = sl_mults[0]
    tp_m  = tp_mults[0]

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

    symbol = symbols[0]
    wft_cutoff = END_DATE - relativedelta(months=WF_TEST_MONTHS)

    print(f"\n対象シンボル : {symbol}")
    print(f"テストパラメータ: {params_dict}")
    print(f"データ期間  : {START_DATE.date()} 〜 {END_DATE.date()}")
    print(f"WFT cutoff  : {wft_cutoff.date()}")
    print(f"スコア重み  : {score_weights}")
    print()

    print(f"[1] データ取得中...")
    try:
        data = get_historical_data(symbol)
        print(f"  データ取得完了: {len(data)}件  ({data.index[0]} 〜 {data.index[-1]})")
    except Exception as e:
        print(f"  [FATAL] データ取得失敗: {e}")
        traceback.print_exc()
        return

    train_data = data[data.index < wft_cutoff]
    print(f"  学習データ: {len(train_data)}件")
    print(f"  テストデータ: {len(data) - len(train_data)}件")

    if len(train_data) < 50:
        print(f"  [WARN] 学習データが少なすぎます({len(train_data)}件)")

    wt_wft    = score_weights.get("wft_sharpe", 0.4)
    wt_is     = score_weights.get("is_sharpe",  0.2)
    wt_pf     = score_weights.get("pf",         0.2)
    wt_trades = score_weights.get("trades",      0.2)

    print(f"\n[2] バックテスト実行中...")
    is_s, wft_r, score, err = _run_single(
        train_data, data, params_dict,
        wt_wft, wt_is, wt_pf, wt_trades,
        debug=True,
    )

    print(f"\n[3] 最終結果")
    print(f"  is_stats  : {is_s}")
    print(f"  wft_result: {wft_r}")
    print(f"  score     : {score:.4f}")
    if err:
        print(f"  error     : {err}")

    print("\n=== デバッグ完了 ===")


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
        # config 読み込み
        config = _cfg
        if os.path.exists(GS_CFG_FILE):
            with open(GS_CFG_FILE, "r", encoding="utf-8") as f:
                gs_cfg = json.load(f)
            score_weights = gs_cfg.get("score_weights") or gs_cfg.get("weights", {})
        else:
            score_weights = {"wft_sharpe": 0.4, "is_sharpe": 0.2, "pf": 0.2, "trades": 0.2}

        if debug:
            debug_run(config, score_weights)
            return

        wt_wft    = score_weights.get("wft_sharpe", 0.4)
        wt_is     = score_weights.get("is_sharpe",  0.2)
        wt_pf     = score_weights.get("pf",         0.2)
        wt_trades = score_weights.get("trades",      0.2)

        symbols = config.get("symbols", SYMBOLS)

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

        wft_cutoff = END_DATE - relativedelta(months=WF_TEST_MONTHS)

        log(f"=== グリッドサーチ開始 ===")
        log(f"対象ペア: {symbols}")
        log(f"組み合わせ数: {len(combos)} × {len(symbols)}銘柄 = {total} 件")
        log(f"スコア重み: WFT={wt_wft} IS={wt_is} PF={wt_pf} 取引={wt_trades}")
        log("")

        error_count = 0

        for symbol in symbols:
            log(f"[{symbol}] データ取得中...")
            try:
                data       = get_historical_data(symbol)
                train_data = data[data.index < wft_cutoff]
                log(f"[{symbol}] データ取得完了: {len(data)}件  学習:{len(train_data)}件")
            except Exception as e:
                log(f"[{symbol}] データ取得失敗: {e}")
                current += len(combos)
                _write_progress(current, total, best_score, best_params,
                                int(time.time() - start_t), 0, log_lines)
                continue

            for i, (bb_p, bb_s, rsi_u, rsi_l, sl_m, tp_m) in enumerate(combos):
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

                is_s, wft_r, score, err_msg = _run_single(
                    train_data, data, params_dict,
                    wt_wft, wt_is, wt_pf, wt_trades,
                )

                # エラー内容をログに記録（最初の10件まで）
                if err_msg and error_count < 10:
                    log(f"  ⚠️ {symbol} combo#{i}: {err_msg}")
                    error_count += 1

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
                    log(f"  ★ 新ベスト: score={best_score:.4f}  {symbol} bb={bb_p}/{bb_s}"
                        f"  rsi={rsi_u}/{rsi_l}  sl={sl_m} tp={tp_m}"
                        f"  取引={row['n_trades']} WFT={row['wft_sharpe']}")

                # 100件ごとに進捗ログ
                if i % 100 == 0 and i > 0:
                    elapsed = int(time.time() - start_t)
                    log(f"  [{symbol}] {i}/{len(combos)} 完了  経過{elapsed}秒")

                # 進捗書き出し
                elapsed   = int(time.time() - start_t)
                remaining = int(elapsed / current * (total - current)) if current else 0
                _write_progress(current, total, best_score, best_params,
                                elapsed, remaining, log_lines)

            log(f"[{symbol}] 完了")

        if error_count > 0:
            log(f"\n⚠️ バックテスト例外合計: {error_count} 件（--debug で詳細確認）")

        # スコア降順ソート
        results.sort(key=lambda x: x["score"], reverse=True)

        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(results[:50], f, indent=2, ensure_ascii=False)

        elapsed = int(time.time() - start_t)
        log(f"\n=== 完了 ===  {total}件  ベストスコア={best_score:.4f}")
        log(f"ベストパラメータ: {best_params}")
        log(f"結果を {RESULTS_FILE} に保存しました。")

        _write_progress(total, total, best_score, best_params, elapsed, 0, log_lines, done=True)
        _update_pid_status("completed")

    except Exception as e:
        tb = traceback.format_exc()
        log(f"予期しないエラー: {e}\n{tb}")
        _update_pid_status("error")
        _write_progress(0, 1, 0.0, {}, 0, 0,
                        log_lines + [f"FATAL: {e}", tb])
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true",
                        help="最初の1パターンのみ実行して詳細デバッグ情報を表示")
    args = parser.parse_args()
    main(debug=args.debug)
