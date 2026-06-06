# 起動: streamlit run dashboard.py

import subprocess
import sys
import time
import streamlit as st
import pandas as pd
import re
import json
import os
from datetime import date, datetime

PARAMS_FILE  = "params.json"
RESULTS_FILE = "backtest_results.json"
LOG_FILE     = "trade_log.txt"
CONFIG_FILE  = "backtest_config.json"

st.set_page_config(page_title="GMO FX Bot Dashboard", layout="wide")
st.title("GMO FX Bot ダッシュボード")


# ==================== データ読込ユーティリティ ====================

def load_params():
    if not os.path.exists(PARAMS_FILE):
        return None
    with open(PARAMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_results():
    if not os.path.exists(RESULTS_FILE):
        return None
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_config() -> dict:
    default = {
        "start_date": "2024-06-16",
        "end_date": "auto",
        "wf_train_months": 12,
        "wf_test_months": 1,
        "symbols": ["EUR_GBP", "AUD_NZD", "EUR_CHF"],
        "bb_period": {"min": 10, "max": 30, "step": 5},
        "bb_std": [1.0, 1.5, 2.0, 2.5],
        "rsi_upper": {"min": 60, "max": 75, "step": 5},
        "rsi_lower": {"min": 25, "max": 40, "step": 5},
        "atr_sl_mult": [1.5, 2.0],
        "atr_tp_mult": [2.0, 2.5],
        "min_trades": 200,
        "min_pf": 1.2,
        "min_wft_sharpe": 0.0,
    }
    if not os.path.exists(CONFIG_FILE):
        return default
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_log():
    records = []
    if not os.path.exists(LOG_FILE):
        return pd.DataFrame(records)
    pat_buy = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
        r"買い注文実行[：:]\s*(\S+)\s+([\d.]+)\S*\s*@\s*\$?([\d.]+)"
    )
    pat_sell = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
        r"(?:売り注文実行|決済注文実行)[：:]\s*(\S+)\s+([\d.]+)"
    )
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat_buy.search(line)
                if m:
                    records.append({"日時": m.group(1), "通貨ペア": m.group(2),
                                    "種別": "買い", "数量": float(m.group(3)),
                                    "価格": float(m.group(4))})
                    continue
                m = pat_sell.search(line)
                if m:
                    records.append({"日時": m.group(1), "通貨ペア": m.group(2),
                                    "種別": "決済", "数量": float(m.group(3)),
                                    "価格": None})
    except Exception as e:
        st.warning(f"ログ読込エラー: {e}")
    return pd.DataFrame(records)


# ==================== タブ構成 ====================

tab_main, tab_config, tab_run, tab_gs = st.tabs([
    "📊 ダッシュボード",
    "⚙️ バックテスト設定",
    "🚀 バックテスト実行",
    "🔍 グリッドサーチ",
])


# ==================== TAB 1: ダッシュボード ====================

with tab_main:

    # 1. サマリーカード
    st.subheader("サマリー")
    params_data = load_params()

    if params_data is None:
        st.warning(f"`{PARAMS_FILE}` が見つかりません。バックテストを実行してください。")
    else:
        params     = params_data.get("params", {})
        excluded   = params_data.get("excluded", [])
        updated_at = params_data.get("updated_at", "—")

        results_data = load_results()
        wft_sharpes, max_dds = [], []
        if results_data:
            for v in results_data.values():
                wft = v.get("wft") or {}
                if wft.get("sharpe") is not None:
                    wft_sharpes.append(wft["sharpe"])
                if wft.get("max_dd") is not None:
                    max_dds.append(wft["max_dd"])

        avg_sharpe = f"{sum(wft_sharpes)/len(wft_sharpes):.2f}" if wft_sharpes else "—"
        avg_dd     = f"{sum(max_dds)/len(max_dds):.1f}%"         if max_dds     else "—"

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("対象ペア数",        len(params))
        c2.metric("平均シャープ(WFT)", avg_sharpe)
        c3.metric("平均最大DD",        avg_dd)
        c4.metric("除外ペア数",        len(excluded))
        c5.metric("最終更新",          updated_at)

    # 2. バックテスト結果テーブル
    st.subheader("最適パラメータ一覧")
    if params_data and params_data.get("params"):
        rows = []
        for sym, p in params_data["params"].items():
            row = {"通貨ペア": sym}
            row.update(p)
            rows.append(row)
        st.dataframe(pd.DataFrame(rows).set_index("通貨ペア"), use_container_width=True)
        if excluded:
            with st.expander("除外ペア"):
                for s in excluded:
                    st.write(f"- {s}")
    else:
        st.info("params.json にパラメータがありません。")

    # 3. エクイティカーブ
    st.subheader("エクイティカーブ")
    results_data = load_results()
    if results_data is None:
        st.info(f"`{RESULTS_FILE}` が見つかりません。バックテストを実行してください。")
    else:
        equity_df = pd.DataFrame()
        for sym, v in results_data.items():
            dates  = v.get("dates", [])
            equity = v.get("equity", [])
            if dates and equity and len(dates) == len(equity):
                s = pd.Series(equity, index=pd.to_datetime(dates), name=sym)
                s = s[~s.index.duplicated(keep="first")]
                equity_df = pd.concat([equity_df, s], axis=1)
                equity_df = equity_df[~equity_df.index.duplicated(keep="first")]
        if equity_df.empty:
            st.info("エクイティデータが空です。")
        else:
            st.line_chart(equity_df)

    # 4. 取引履歴
    st.subheader("取引履歴")
    df_log = load_log()
    if df_log.empty:
        st.info(f"`{LOG_FILE}` に取引記録がありません。")
    else:
        st.dataframe(df_log, use_container_width=True)
        c1, c2 = st.columns(2)
        c1.metric("買い注文 合計", int((df_log["種別"] == "買い").sum()))
        c2.metric("決済注文 合計", int((df_log["種別"] == "決済").sum()))
        st.markdown("**通貨ペア別 取引回数**")
        count_df = (
            df_log.groupby(["通貨ペア", "種別"]).size()
            .reset_index(name="回数")
            .pivot(index="通貨ペア", columns="種別", values="回数")
            .fillna(0).astype(int)
        )
        st.bar_chart(count_df)


# ==================== TAB 2: バックテスト設定 ====================

with tab_config:
    st.subheader("バックテスト設定")
    cfg = load_config()

    with st.form("config_form"):
        # ── 通貨ペア選択 ──────────────────────────────────────────────────
        st.markdown("#### 取引通貨ペア")
        available = cfg.get("available_symbols", [
            "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY",
            "NZD_JPY", "CAD_JPY", "CHF_JPY", "ZAR_JPY",
            "EUR_USD", "GBP_USD", "AUD_USD", "EUR_GBP",
            "AUD_NZD", "EUR_CHF", "GBP_CHF", "EUR_AUD",
        ])
        current_symbols = set(cfg.get("symbols", []))
        # 4列グリッドでチェックボックスを表示
        symbol_checks: dict[str, bool] = {}
        cols = st.columns(4)
        for i, sym in enumerate(available):
            with cols[i % 4]:
                symbol_checks[sym] = st.checkbox(
                    sym,
                    value=(sym in current_symbols),
                    key=f"sym_{sym}",
                )

        st.markdown("#### 基本設定")
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input(
                "開始日",
                value=date.fromisoformat(cfg.get("start_date", "2024-06-16")),
            )
        with col2:
            st.text_input("終了日", value="auto（昨日）", disabled=True)

        col3, col4 = st.columns(2)
        with col3:
            wf_train = st.slider("学習期間（ヶ月）", 1, 24,
                                  value=int(cfg.get("wf_train_months", 12)))
        with col4:
            wf_test  = st.slider("WFT検証期間（ヶ月）", 1, 6,
                                  value=int(cfg.get("wf_test_months", 1)))

        st.markdown("#### 最適化パラメータ範囲")

        # BB期間
        st.markdown("**BB期間 (bb_period)**")
        bb_col1, bb_col2, bb_col3 = st.columns(3)
        bb_min  = bb_col1.number_input("min", value=int(cfg["bb_period"]["min"]),  min_value=1,  step=1, key="bb_min")
        bb_max  = bb_col2.number_input("max", value=int(cfg["bb_period"]["max"]),  min_value=1,  step=1, key="bb_max")
        bb_step = bb_col3.number_input("step", value=int(cfg["bb_period"]["step"]), min_value=1, step=1, key="bb_step")

        # BB標準偏差
        bb_std = st.multiselect(
            "BB標準偏差 (bb_std)",
            options=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
            default=cfg.get("bb_std", [1.0, 1.5, 2.0, 2.5]),
        )

        # RSI upper
        st.markdown("**RSI上限 (rsi_upper)**")
        ru_col1, ru_col2 = st.columns(2)
        rsi_upper_min = ru_col1.number_input("min", value=int(cfg["rsi_upper"]["min"]), min_value=50, max_value=95, step=5, key="ru_min")
        rsi_upper_max = ru_col2.number_input("max", value=int(cfg["rsi_upper"]["max"]), min_value=50, max_value=95, step=5, key="ru_max")

        # RSI lower
        st.markdown("**RSI下限 (rsi_lower)**")
        rl_col1, rl_col2 = st.columns(2)
        rsi_lower_min = rl_col1.number_input("min", value=int(cfg["rsi_lower"]["min"]), min_value=5, max_value=50, step=5, key="rl_min")
        rsi_lower_max = rl_col2.number_input("max", value=int(cfg["rsi_lower"]["max"]), min_value=5, max_value=50, step=5, key="rl_max")

        # ATR倍率
        atr_sl = st.multiselect(
            "ATR損切倍率 (atr_sl_mult)",
            options=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
            default=cfg.get("atr_sl_mult", [1.5, 2.0]),
        )
        atr_tp = st.multiselect(
            "ATR利確倍率 (atr_tp_mult)",
            options=[1.0, 1.5, 2.0, 2.5, 3.0, 4.0],
            default=cfg.get("atr_tp_mult", [2.0, 2.5]),
        )

        st.markdown("#### 除外条件")
        ex_col1, ex_col2, ex_col3 = st.columns(3)
        min_trades    = ex_col1.number_input("最低取引回数",        value=int(cfg.get("min_trades",     200)), min_value=0)
        min_pf        = ex_col2.number_input("最低 Profit Factor", value=float(cfg.get("min_pf",       1.2)), min_value=0.0, step=0.1, format="%.1f")
        min_wft_sharpe = ex_col3.number_input("最低WFTシャープ",    value=float(cfg.get("min_wft_sharpe", 0.0)), step=0.1, format="%.1f")

        submitted = st.form_submit_button("💾 設定を保存")

    if submitted:
        selected_symbols = [s for s, checked in symbol_checks.items() if checked]

        if not selected_symbols:
            st.warning("⚠️ 最低1つの通貨ペアを選択してください。")
        else:
            new_cfg = {
                "available_symbols": available,
                "start_date":        start_date.isoformat(),
                "end_date":          "auto",
                "wf_train_months":   wf_train,
                "wf_test_months":    wf_test,
                "symbols":           selected_symbols,
                "bb_period":         {"min": int(bb_min), "max": int(bb_max), "step": int(bb_step)},
                "bb_std":            sorted(bb_std) if bb_std else [2.0],
                "rsi_upper":         {"min": int(rsi_upper_min), "max": int(rsi_upper_max), "step": 5},
                "rsi_lower":         {"min": int(rsi_lower_min), "max": int(rsi_lower_max), "step": 5},
                "atr_sl_mult":       sorted(atr_sl) if atr_sl else [1.5],
                "atr_tp_mult":       sorted(atr_tp) if atr_tp else [2.0],
                "min_trades":        int(min_trades),
                "min_pf":            float(min_pf),
                "min_wft_sharpe":    float(min_wft_sharpe),
            }
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            st.success(f"✅ 設定を保存しました（選択ペア: {', '.join(selected_symbols)}）")
            st.json(new_cfg)


# ==================== TAB 3: バックテスト実行 ====================

with tab_run:
    st.subheader("バックテスト実行")

    # session_state の初期化
    if "bt_process"  not in st.session_state:
        st.session_state.bt_process  = None
    if "bt_running"  not in st.session_state:
        st.session_state.bt_running  = False
    if "bt_log_lines" not in st.session_state:
        st.session_state.bt_log_lines = []

    # 実行中でなければ [▶ 開始] ボタン
    if not st.session_state.bt_running:
        if st.button("▶ バックテスト開始", type="primary"):
            st.session_state.bt_log_lines = []
            backtest_script = os.path.join(os.path.dirname(__file__) or ".", "backtest.py")
            proc = subprocess.Popen(
                [sys.executable, "-u", backtest_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
            st.session_state.bt_process = proc
            st.session_state.bt_running = True
            st.rerun()

    # 実行中: ログ表示 + [⏹ 停止] ボタン
    if st.session_state.bt_running:
        proc = st.session_state.bt_process

        col_stop, _ = st.columns([1, 4])
        with col_stop:
            if st.button("⏹ 停止"):
                if proc and proc.poll() is None:
                    proc.terminate()
                st.session_state.bt_running  = False
                st.session_state.bt_process  = None
                st.warning("バックテストを停止しました。")
                st.rerun()

        log_box = st.empty()

        # ポーリングしてログを収集
        if proc and proc.poll() is None:
            try:
                for _ in range(20):   # 0.5秒 × 20 = 最大10秒分を1回のレンダリングで取得
                    line = proc.stdout.readline()
                    if line:
                        st.session_state.bt_log_lines.append(line.rstrip())
                    else:
                        time.sleep(0.05)
            except Exception:
                pass

        log_box.code("\n".join(st.session_state.bt_log_lines[-200:]), language="")

        # プロセス終了チェック
        if proc and proc.poll() is not None:
            # 残りの出力を全部読む
            try:
                remaining = proc.stdout.read()
                if remaining:
                    st.session_state.bt_log_lines.extend(remaining.splitlines())
            except Exception:
                pass

            log_box.code("\n".join(st.session_state.bt_log_lines[-200:]), language="")
            st.session_state.bt_running = False
            st.session_state.bt_process = None

            if proc.returncode == 0:
                st.success("✅ バックテスト完了！")
                st.balloons()
            else:
                st.error(f"⚠️ バックテストがエラー終了しました（終了コード: {proc.returncode}）")

            st.rerun()

        # 実行中は自動リフレッシュ
        else:
            time.sleep(0.5)
            st.rerun()

    # 完了後のログ表示（実行中でなく、ログがある場合）
    elif st.session_state.bt_log_lines:
        st.markdown("**前回の実行ログ**")
        st.code("\n".join(st.session_state.bt_log_lines[-200:]), language="")


# ==================== TAB 4: グリッドサーチ ====================

GS_PROGRESS_FILE = "grid_search_progress.json"
GS_RESULTS_FILE  = "grid_search_results.json"
GS_CONFIG_FILE   = "grid_search_config.json"

with tab_gs:
    st.subheader("グリッドサーチ")

    # session_state 初期化
    for _k, _v in [("gs_process", None), ("gs_running", False), ("gs_done", False)]:
        if _k not in st.session_state:
            st.session_state[_k] = _v

    # ── スコアリング重み設定 ──────────────────────────────────────────────
    st.markdown("#### スコアリング重み")
    w_col1, w_col2, w_col3, w_col4 = st.columns(4)
    wt_wft    = w_col1.slider("WFTシャープ", 0.0, 1.0, 0.4, 0.05, key="wt_wft")
    wt_is     = w_col2.slider("ISシャープ",  0.0, 1.0, 0.2, 0.05, key="wt_is")
    wt_pf     = w_col3.slider("PF",          0.0, 1.0, 0.2, 0.05, key="wt_pf")
    wt_trades = w_col4.slider("取引回数",    0.0, 1.0, 0.2, 0.05, key="wt_trades")

    total_w = wt_wft + wt_is + wt_pf + wt_trades
    st.caption(f"合計: **{total_w:.2f}**")
    if total_w > 1.001:
        st.warning(f"⚠️ 重みの合計が {total_w:.2f} です。合計が 1.0 を超えています。")

    # ── 開始 / 停止ボタン ────────────────────────────────────────────────
    if not st.session_state.gs_running:
        if st.button("🔍 グリッドサーチ開始", type="primary"):
            # grid_search_config.json にスコア重みを保存
            gs_cfg = {
                "score_weights": {
                    "wft_sharpe": wt_wft,
                    "is_sharpe":  wt_is,
                    "pf":         wt_pf,
                    "trades":     wt_trades,
                }
            }
            with open(GS_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(gs_cfg, f, indent=2)

            # progress ファイルをリセット
            if os.path.exists(GS_PROGRESS_FILE):
                os.remove(GS_PROGRESS_FILE)

            backtest_script = os.path.join(os.path.dirname(__file__) or ".", "backtest.py")
            proc = subprocess.Popen(
                [sys.executable, "-u", backtest_script, "--grid-search"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
            st.session_state.gs_process = proc
            st.session_state.gs_running = True
            st.session_state.gs_done    = False
            st.rerun()

    if st.session_state.gs_running:
        proc = st.session_state.gs_process

        # 停止ボタン
        if st.button("⏹ 停止", key="gs_stop"):
            if proc and proc.poll() is None:
                proc.terminate()
            st.session_state.gs_running = False
            st.session_state.gs_process = None
            st.warning("グリッドサーチを停止しました。")
            st.rerun()

        # 進捗バー表示
        prog_placeholder  = st.empty()
        stats_placeholder = st.empty()
        best_placeholder  = st.empty()

        if os.path.exists(GS_PROGRESS_FILE):
            try:
                with open(GS_PROGRESS_FILE, "r", encoding="utf-8") as f:
                    prog = json.load(f)
                cur   = prog.get("current", 0)
                tot   = prog.get("total",   1)
                ratio = cur / tot if tot > 0 else 0.0

                with prog_placeholder.container():
                    st.progress(ratio)
                    elapsed   = prog.get("elapsed",   0)
                    remaining = prog.get("remaining", 0)
                    st.caption(
                        f"{cur} / {tot} 完了　"
                        f"経過: {elapsed//60}分{elapsed%60}秒　"
                        f"残り: {remaining//60}分{remaining%60}秒"
                    )

                with stats_placeholder.container():
                    st.metric("現在ベストスコア", f"{prog.get('best_score', 0):.4f}")

                with best_placeholder.container():
                    if prog.get("best_params"):
                        st.json(prog["best_params"])

            except Exception:
                pass

        # プロセス終了チェック
        if proc and proc.poll() is not None:
            st.session_state.gs_running = False
            st.session_state.gs_process = None
            st.session_state.gs_done    = True
            if proc.returncode == 0:
                st.success("✅ グリッドサーチ完了！")
                st.balloons()
            else:
                st.error(f"⚠️ エラー終了（終了コード: {proc.returncode}）")
            st.rerun()
        else:
            time.sleep(0.5)
            st.rerun()

    # ── 結果テーブル ─────────────────────────────────────────────────────
    if os.path.exists(GS_RESULTS_FILE):
        st.markdown("#### 検索結果（上位20件）")
        try:
            with open(GS_RESULTS_FILE, "r", encoding="utf-8") as f:
                gs_rows = json.load(f)

            df_gs = pd.DataFrame(gs_rows[:20])
            display_gs_cols = [
                "symbol", "bb_period", "bb_std", "rsi_upper", "rsi_lower",
                "atr_sl_mult", "atr_tp_mult", "n_trades", "pf",
                "is_sharpe", "wft_sharpe", "score",
            ]
            display_gs_cols = [c for c in display_gs_cols if c in df_gs.columns]
            df_gs_display = df_gs[display_gs_cols].copy()

            # ベスト行を強調（score 最大行に ★ を付ける）
            if not df_gs_display.empty and "score" in df_gs_display.columns:
                best_idx = df_gs_display["score"].idxmax()
                df_gs_display.insert(0, "rank", "")
                df_gs_display.loc[best_idx, "rank"] = "★"

            st.dataframe(df_gs_display, use_container_width=True)

            # ── ベストパラメータ採用 ─────────────────────────────────────
            if gs_rows:
                best_row = gs_rows[0]
                st.markdown(f"**ベストスコア: {best_row['score']:.4f}** / 銘柄: {best_row.get('symbol','')}")

                if st.button("✅ ベストパラメータを採用"):
                    symbol = best_row.get("symbol", "")
                    new_p  = {
                        k: best_row[k]
                        for k in ["bb_period","bb_std","rsi_period","rsi_upper","rsi_lower",
                                  "atr_period","atr_sl_mult","atr_tp_mult"]
                        if k in best_row
                    }

                    # params.json 更新
                    params_data = load_params() or {}
                    params_data.setdefault("params", {})[symbol] = new_p
                    params_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
                        json.dump(params_data, f, indent=2, ensure_ascii=False)

                    # backtest_config.json の symbols にも反映
                    cfg_now = load_config()
                    if symbol and symbol not in cfg_now.get("symbols", []):
                        cfg_now.setdefault("symbols", []).append(symbol)
                        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                            json.dump(cfg_now, f, indent=2, ensure_ascii=False)

                    st.success(f"✅ {symbol} のパラメータを更新しました。📊タブを確認してください。")

        except Exception as e:
            st.error(f"結果ファイル読み込みエラー: {e}")
