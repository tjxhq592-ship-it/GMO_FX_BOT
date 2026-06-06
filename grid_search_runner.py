"""
グリッドサーチ独立実行スクリプト
起動コマンド: python grid_search_runner.py

dashboard.py とは完全に独立したプロセスとして動作する。
進捗は grid_search_progress.json にリアルタイムで書き出す。
"""
import json
import os
import sys
import time
import itertools
from datetime import datetime
from dateutil.relativedelta import relativedelta

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


def main() -> None:
    # PID を記録
    _write_pid("running")
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
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

        for symbol in symbols:
            log(f"[{symbol}] データ取得中...")
            try:
                data       = get_historical_data(symbol)
                train_data = data[data.index < wft_cutoff]
                log(f"[{symbol}] データ取得完了: {len(data)}件")
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

                # IS バックテスト
                try:
                    bt    = Backtest(train_data, ImprovedStrategy,
                                     cash=INITIAL_CASH, commission=0.00002)
                    st_is = bt.run(**params_dict)
                    is_s  = _extract_stats(st_is)
                except Exception:
                    is_s = None

                # WFT
                wft_r = walk_forward_test(data, params_dict) if is_s else None

                # スコアリング
                score = 0.0
                if is_s:
                    n          = is_s["n_trades"]
                    pf         = is_s["pf"] or 0.0
                    is_sharpe  = is_s["sharpe"]
                    wft_sharpe = wft_r["sharpe"] if wft_r else float("nan")

                    nan_check = wft_sharpe != wft_sharpe  # NaN 判定
                    if n >= 50 and not nan_check and wft_sharpe >= 0:
                        score = (
                            wft_sharpe           * wt_wft +
                            max(is_sharpe, 0.0)  * wt_is  +
                            max(pf, 0.0)         * wt_pf  +
                            min(n / 200.0, 1.0)  * wt_trades
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
        log(f"予期しないエラー: {e}")
        _update_pid_status("error")
        _write_progress(0, 1, 0.0, {}, 0, 0,
                        log_lines + [f"ERROR: {e}"])
        sys.exit(1)


if __name__ == "__main__":
    main()
