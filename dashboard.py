# 起動: streamlit run dashboard.py

import signal
import subprocess
import sys
import time
import psutil
import streamlit as st
import pandas as pd
import re
import json
import os
from datetime import date, datetime
from streamlit_autorefresh import st_autorefresh

# スクリプトと同じディレクトリを基準ディレクトリとして使用（相対パス問題を回避）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

PARAMS_FILE      = os.path.join(BASE_DIR, "params.json")
RESULTS_FILE     = os.path.join(BASE_DIR, "backtest_results.json")
LOG_FILE         = os.path.join(BASE_DIR, "trade_log.txt")
CONFIG_FILE      = os.path.join(BASE_DIR, "backtest_config.json")
GS_PROGRESS_FILE = os.path.join(BASE_DIR, "grid_search_progress.json")
GS_RESULTS_FILE  = os.path.join(BASE_DIR, "grid_search_results.json")
GS_CONFIG_FILE   = os.path.join(BASE_DIR, "grid_search_config.json")
GS_PID_FILE      = os.path.join(BASE_DIR, "grid_search_pid.json")
BT_PROGRESS_FILE = os.path.join(BASE_DIR, "backtest_progress.json")
BT_PID_FILE      = os.path.join(BASE_DIR, "backtest_pid.json")


def _launch_detached(cmd: list[str]) -> subprocess.Popen:
    """OS に応じてデタッチドプロセスを起動する"""
    if sys.platform == "win32":
        return subprocess.Popen(
            cmd,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            cwd=BASE_DIR,
        )
    else:
        return subprocess.Popen(cmd, start_new_session=True, cwd=BASE_DIR)


def _kill_pid(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.call(["taskkill", "/F", "/PID", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def _read_pid_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_progress_json(path: str, retries: int = 3, delay: float = 0.1) -> dict | None:
    if not os.path.exists(path):
        return None
    if os.path.getsize(path) == 0:
        return None
    for attempt in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            if attempt < retries - 1:
                time.sleep(delay)
        except Exception:
            return None
    return None


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
        "symbols": ["AUD_NZD"],
        "active_symbols": ["AUD_NZD"],
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

def load_gs_config() -> dict:
    default = {
        "score_weights": {"wft_sharpe": 0.4, "is_sharpe": 0.2, "pf": 0.2, "trades": 0.2},
        "max_workers": 1,
    }
    if not os.path.exists(GS_CONFIG_FILE):
        return default
    try:
        with open(GS_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 旧フォーマット互換
        if "weights" in data and "score_weights" not in data:
            data["score_weights"] = data.pop("weights")
        return {**default, **data}
    except Exception:
        return default

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
    "⚙️ 設定",
    "🚀 バックテスト実行",
    "🔍 グリッドサーチ",
])


# ==================== TAB 1: ダッシュボード ====================

with tab_main:

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


# ==================== TAB 2: 設定 ====================

with tab_config:
    st.subheader("設定")
    cfg      = load_config()
    gs_cfg   = load_gs_config()
    sw       = gs_cfg.get("score_weights", {})
    _cpu_max = max(1, (os.cpu_count() or 4) - 2)

    _available = cfg.get("available_symbols", [
        "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY",
        "NZD_JPY", "CAD_JPY", "CHF_JPY", "ZAR_JPY",
        "EUR_USD", "GBP_USD", "AUD_USD", "EUR_GBP",
        "AUD_NZD", "EUR_CHF", "GBP_CHF", "EUR_AUD",
    ])
    _params_now     = load_params() or {}
    _adopted_syms   = list(_params_now.get("params", {}).keys())
    _active_syms    = cfg.get("active_symbols", cfg.get("symbols", []))
    _gs_syms        = cfg.get("grid_search_symbols", [])

    with st.form("settings_form"):

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 📌 共通設定
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        st.markdown("### 📌 共通設定")

        st.markdown("#### データ期間")
        d_col1, d_col2 = st.columns(2)
        with d_col1:
            start_date = st.date_input(
                "開始日",
                value=date.fromisoformat(cfg.get("start_date", "2024-06-16")),
            )
        with d_col2:
            st.text_input("終了日", value="auto（昨日）", disabled=True)

        st.markdown("---")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 🔍 グリッドサーチ設定
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        st.markdown("### 🔍 グリッドサーチ設定")

        st.markdown("#### 対象通貨ペア")
        st.info(
            "グリッドサーチを実施する通貨ペアを選択してください。"
            "結果上位N件がトレード対象に採用されます。"
        )
        gs_new_syms: list[str] = []
        gs_sym_cols = st.columns(4)
        for _i, _sym in enumerate(_available):
            with gs_sym_cols[_i % 4]:
                _checked = st.checkbox(
                    _sym,
                    value=(_sym in _gs_syms),
                    key=f"gs_sym_{_sym}",
                )
                if _checked:
                    gs_new_syms.append(_sym)

        st.markdown("#### 採用銘柄数")
        gs_top_n = st.number_input(
            "上位N銘柄を採用",
            min_value=1,
            max_value=10,
            value=int(cfg.get("grid_search_top_n", 3)),
            help="グリッドサーチ結果のスコア上位N件をトレード対象に採用",
        )

        st.markdown("#### 探索パラメータ範囲")

        st.markdown("**BB期間 (bb_period)**")
        bb_col1, bb_col2, bb_col3 = st.columns(3)
        bb_min  = bb_col1.number_input("min",  value=int(cfg.get("bb_period", {}).get("min",  10)), min_value=1, step=1, key="bb_min")
        bb_max  = bb_col2.number_input("max",  value=int(cfg.get("bb_period", {}).get("max",  30)), min_value=1, step=1, key="bb_max")
        bb_step = bb_col3.number_input("step", value=int(cfg.get("bb_period", {}).get("step",  5)), min_value=1, step=1, key="bb_step")

        bb_std = st.multiselect(
            "BB標準偏差 (bb_std)",
            options=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
            default=cfg.get("bb_std", [1.0, 1.5, 2.0, 2.5]),
        )

        st.markdown("**RSI上限 (rsi_upper)**")
        ru_col1, ru_col2, ru_col3 = st.columns(3)
        rsi_upper_min  = ru_col1.number_input("min",  value=int(cfg.get("rsi_upper", {}).get("min",  60)), min_value=50, max_value=95, step=5, key="ru_min")
        rsi_upper_max  = ru_col2.number_input("max",  value=int(cfg.get("rsi_upper", {}).get("max",  75)), min_value=50, max_value=95, step=5, key="ru_max")
        rsi_upper_step = ru_col3.number_input("step", value=int(cfg.get("rsi_upper", {}).get("step",  5)), min_value=1,  step=1,       key="ru_step")

        st.markdown("**RSI下限 (rsi_lower)**")
        rl_col1, rl_col2, rl_col3 = st.columns(3)
        rsi_lower_min  = rl_col1.number_input("min",  value=int(cfg.get("rsi_lower", {}).get("min",  25)), min_value=5, max_value=50, step=5, key="rl_min")
        rsi_lower_max  = rl_col2.number_input("max",  value=int(cfg.get("rsi_lower", {}).get("max",  40)), min_value=5, max_value=50, step=5, key="rl_max")
        rsi_lower_step = rl_col3.number_input("step", value=int(cfg.get("rsi_lower", {}).get("step",  5)), min_value=1, step=1,       key="rl_step")

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

        st.markdown("#### 並列ワーカー数")
        _saved_mw = max(1, min(int(gs_cfg.get("max_workers", 1)), _cpu_max))
        max_workers_val = st.slider(
            "並列ワーカー数",
            min_value=1,
            max_value=_cpu_max,
            value=_saved_mw,
            help=f"グリッドサーチ・バックテストの並列処理数。CPUコア数({os.cpu_count()})の上限-2={_cpu_max}まで設定可能。大きいほど速いが負荷も高い",
            key="gs_max_workers",
        )

        st.markdown("#### スコアリング重み")
        wt_col1, wt_col2, wt_col3, wt_col4 = st.columns(4)
        wt_wft    = wt_col1.slider("WFTシャープ", 0.0, 1.0, float(sw.get("wft_sharpe", 0.4)), 0.05, key="wt_wft")
        wt_is     = wt_col2.slider("ISシャープ",  0.0, 1.0, float(sw.get("is_sharpe",  0.2)), 0.05, key="wt_is")
        wt_pf     = wt_col3.slider("PF",          0.0, 1.0, float(sw.get("pf",         0.2)), 0.05, key="wt_pf")
        wt_trades = wt_col4.slider("取引回数",    0.0, 1.0, float(sw.get("trades",     0.2)), 0.05, key="wt_trades")
        total_w = wt_wft + wt_is + wt_pf + wt_trades
        st.caption(f"合計: **{total_w:.2f}**")
        if total_w > 1.001:
            st.warning(f"⚠️ 重みの合計が {total_w:.2f} です。1.0 を超えています。")

        st.markdown("---")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 📊 バックテスト・トレード設定
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        st.markdown("### 📊 バックテスト・トレード設定")

        st.markdown("#### 対象通貨ペア")
        st.info(
            "グリッドサーチで採用されたペアが自動設定されます。"
            "採用済みペアの手動除外のみ可能です。"
        )
        bt_new_active: list[str] = []
        bt_sym_cols = st.columns(4)
        for _i, _sym in enumerate(_available):
            _is_adopted = _sym in _adopted_syms
            _is_active  = _sym in _active_syms
            with bt_sym_cols[_i % 4]:
                if _is_adopted:
                    _cb = st.checkbox(
                        _sym,
                        value=_is_active,
                        key=f"bt_sym_{_sym}",
                    )
                    if _cb:
                        bt_new_active.append(_sym)
                else:
                    st.checkbox(
                        _sym,
                        value=False,
                        disabled=True,
                        key=f"bt_sym_{_sym}",
                        help="グリッドサーチで採用されていません",
                    )

        st.markdown("#### WFT設定")
        wf_col1, wf_col2 = st.columns(2)
        with wf_col1:
            wf_train = st.slider("WFT学習期間（ヶ月）", 6, 24,
                                  value=int(cfg.get("wf_train_months", 12)))
        with wf_col2:
            wf_test = st.slider("WFT検証期間（ヶ月）", 1, 6,
                                 value=int(cfg.get("wf_test_months", 1)))

        st.markdown("#### 除外条件")
        ex_col1, ex_col2, ex_col3 = st.columns(3)
        min_trades     = ex_col1.number_input("最小取引回数",    value=int(cfg.get("min_trades",       100)), min_value=0)
        min_pf         = ex_col2.number_input("最小PF",         value=float(cfg.get("min_pf",          1.2)), min_value=0.0, step=0.1, format="%.1f")
        min_wft_sharpe = ex_col3.number_input("最小WFTシャープ", value=float(cfg.get("min_wft_sharpe", -0.3)), step=0.1, format="%.1f")

        st.markdown("---")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 保存ボタン（1つだけ）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        submitted = st.form_submit_button("💾 設定を保存", type="primary", use_container_width=True)

    if submitted:
        errors = []

        # ── backtest_config.json に保存 ──────────────────────────────────
        existing_cfg = load_config()
        new_cfg = {
            **existing_cfg,
            "available_symbols":   _available,
            "start_date":          start_date.isoformat(),
            "end_date":            "auto",
            "grid_search_symbols": gs_new_syms,
            "grid_search_top_n":   int(gs_top_n),
            "wf_train_months":     wf_train,
            "wf_test_months":      wf_test,
            "active_symbols":      bt_new_active,
            "symbols":             bt_new_active,   # 後方互換
            "bb_period":           {"min": int(bb_min),  "max": int(bb_max),  "step": int(bb_step)},
            "bb_std":              sorted(bb_std)  if bb_std  else [2.0],
            "rsi_upper":           {"min": int(rsi_upper_min), "max": int(rsi_upper_max), "step": int(rsi_upper_step)},
            "rsi_lower":           {"min": int(rsi_lower_min), "max": int(rsi_lower_max), "step": int(rsi_lower_step)},
            "atr_sl_mult":         sorted(atr_sl)  if atr_sl  else [1.5],
            "atr_tp_mult":         sorted(atr_tp)  if atr_tp  else [2.0],
            "min_trades":          int(min_trades),
            "min_pf":              float(min_pf),
            "min_wft_sharpe":      float(min_wft_sharpe),
            "max_workers":         max_workers_val,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            errors.append(f"backtest_config.json 保存エラー: {e}")

        # ── grid_search_config.json に保存 ───────────────────────────────
        existing_gs = load_gs_config()
        existing_gs["score_weights"] = {
            "wft_sharpe": wt_wft, "is_sharpe": wt_is,
            "pf": wt_pf, "trades": wt_trades,
        }
        existing_gs["max_workers"] = max_workers_val
        try:
            with open(GS_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_gs, f, indent=2, ensure_ascii=False)
        except Exception as e:
            errors.append(f"grid_search_config.json 保存エラー: {e}")

        if errors:
            for err in errors:
                st.error(err)
        else:
            st.success(
                f"✅ 保存しました  "
                f"GS対象: {', '.join(gs_new_syms) or '(未選択)'}  /  "
                f"アクティブ: {', '.join(bt_new_active) or '(なし)'}  /  "
                f"max_workers: {max_workers_val}"
            )
            with st.expander("保存内容を確認"):
                c_left, c_right = st.columns(2)
                with c_left:
                    st.caption("backtest_config.json")
                    st.json(load_config())
                with c_right:
                    st.caption("grid_search_config.json")
                    st.json(load_gs_config())


# ==================== TAB 3: バックテスト実行 ====================

with tab_run:
    st.subheader("バックテスト実行")
    st_autorefresh(interval=3000, key="bt_refresh")

    bt_pid_data  = _read_pid_file(BT_PID_FILE)
    bt_pid       = bt_pid_data.get("pid")
    bt_status    = bt_pid_data.get("status", "")
    _bt_alive    = bool(bt_pid and psutil.pid_exists(int(bt_pid)))
    bt_running   = (bt_status == "running") and _bt_alive

    if bt_status == "running" and not _bt_alive and os.path.exists(BT_PID_FILE):
        bt_pid_data["status"] = "stopped"
        with open(BT_PID_FILE, "w", encoding="utf-8") as f:
            json.dump(bt_pid_data, f, indent=2)

    if bt_running:
        st.info(f"⏳ バックテスト実行中  (PID: {bt_pid}　開始: {bt_pid_data.get('started_at','')})")
        if st.button("⏹ 停止", key="bt_stop"):
            if bt_pid and _kill_pid(bt_pid):
                bt_pid_data["status"] = "stopped"
                with open(BT_PID_FILE, "w", encoding="utf-8") as f:
                    json.dump(bt_pid_data, f, indent=2)
                st.warning("バックテストを停止しました。")
            else:
                st.error("プロセスの停止に失敗しました。")
    else:
        if st.button("▶ バックテスト開始", type="primary", key="bt_start"):
            try:
                proc = _launch_detached(["python", "backtest.py"])
                pid_data = {
                    "pid":        proc.pid,
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status":     "running",
                }
                with open(BT_PID_FILE, "w", encoding="utf-8") as f:
                    json.dump(pid_data, f, indent=2)
                if os.path.exists(BT_PROGRESS_FILE):
                    os.remove(BT_PROGRESS_FILE)
                st.success(f"バックテストを起動しました (PID: {proc.pid})")
            except Exception as e:
                st.error(f"起動失敗: {e}")
        st.caption("または、別のターミナルで:")
        st.code("python backtest.py", language="bash")

    bt_prog = _read_progress_json(BT_PROGRESS_FILE)
    if bt_prog is not None:
        status = bt_prog.get("status", "running")
        cur    = bt_prog.get("current", 0)
        tot    = bt_prog.get("total_symbols", 1)
        symbol = bt_prog.get("current_symbol", "")
        ratio  = cur / tot if tot > 0 else 0.0
        logs   = bt_prog.get("log", [])

        if status == "completed":
            st.success("✅ バックテスト完了！")
        elif status == "error":
            st.error("⚠️ エラーで終了しました。ログを確認してください。")
        else:
            st.progress(ratio)
            st.caption(f"{cur} / {tot} 銘柄完了　現在処理中: {symbol}")

        if logs:
            st.text_area("実行ログ（最新20件）",
                         value="\n".join(logs[-20:]),
                         height=300, disabled=True, key="bt_log_area")
    else:
        st.caption("`backtest_progress.json` がありません。実行開始後に進捗が表示されます。")


# ==================== TAB 4: グリッドサーチ ====================

with tab_gs:
    st.subheader("グリッドサーチ")
    st_autorefresh(interval=3000, key="gs_refresh")

    # ── 実行制御 ──────────────────────────────────────────────────────────
    # PIDファイルを読み込み、プロセスが実際に生きているか psutil で確認
    gs_pid_data = _read_pid_file(GS_PID_FILE)
    gs_pid      = gs_pid_data.get("pid")
    gs_status   = gs_pid_data.get("status", "")
    _proc_alive = bool(gs_pid and psutil.pid_exists(int(gs_pid)))
    gs_running  = ((gs_status == "running") and _proc_alive) \
                  or st.session_state.get("gs_force_running", False)

    # PIDファイルが "running" でもプロセスが死んでいれば自動修正
    if gs_status == "running" and not _proc_alive and os.path.exists(GS_PID_FILE):
        gs_pid_data["status"] = "stopped"
        with open(GS_PID_FILE, "w", encoding="utf-8") as f:
            json.dump(gs_pid_data, f, indent=2)
        st.session_state.pop("gs_force_running", None)
    elif _proc_alive:
        # プロセスが確認できたら強制フラグをクリア
        st.session_state.pop("gs_force_running", None)

    if gs_running:
        st.info(f"⏳ グリッドサーチ実行中  (PID: {gs_pid}　開始: {gs_pid_data.get('started_at','')})")
        if st.button("⏹ 停止", key="gs_stop"):
            if gs_pid and _kill_pid(gs_pid):
                gs_pid_data["status"] = "stopped"
                with open(GS_PID_FILE, "w", encoding="utf-8") as f:
                    json.dump(gs_pid_data, f, indent=2)
                st.warning("グリッドサーチを停止しました。")
            else:
                st.error("プロセスの停止に失敗しました。")
    else:
        if st.button("▶ 開始", type="primary", key="gs_start"):
            try:
                proc = _launch_detached(["python", "grid_search_runner.py"])
                _init_pid = {
                    "pid":        proc.pid,
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status":     "running",
                }
                with open(GS_PID_FILE, "w", encoding="utf-8") as f:
                    json.dump(_init_pid, f, indent=2)
                if os.path.exists(GS_PROGRESS_FILE):
                    os.remove(GS_PROGRESS_FILE)

                # プロセスが起動するまで最大10秒待機（st.rerun はループ完了後に呼ぶ）
                for _ in range(20):
                    time.sleep(0.5)
                    if os.path.exists(GS_PID_FILE):
                        try:
                            with open(GS_PID_FILE, "r", encoding="utf-8") as _f:
                                _pid_check = json.load(_f)
                            if _pid_check.get("status") == "running":
                                st.session_state["gs_force_running"] = True
                                break
                        except Exception:
                            pass

                st.success(f"グリッドサーチを起動しました (PID: {proc.pid})")
                st.rerun()
            except Exception as e:
                st.error(f"起動失敗: {e}")
        st.caption("または、別のターミナルで:")
        st.code("python grid_search_runner.py", language="bash")

    # ── 実行状況 ──────────────────────────────────────────────────────────
    st.markdown("#### 実行状況")
    gs_prog = _read_progress_json(GS_PROGRESS_FILE)
    if gs_prog is not None:
        status    = gs_prog.get("status", "running")
        cur       = gs_prog.get("current", 0)
        tot       = gs_prog.get("total",   1)
        ratio     = cur / tot if tot > 0 else 0.0
        elapsed   = gs_prog.get("elapsed",   0)
        remaining = gs_prog.get("remaining", 0)
        best_s    = gs_prog.get("best_score",  0)
        best_p    = gs_prog.get("best_params", {})
        logs      = gs_prog.get("log", [])

        if status == "completed":
            st.success("✅ グリッドサーチ完了！")
        elif status == "error":
            st.error("⚠️ エラーで終了しました。")
        else:
            st.progress(ratio)
            st.caption(
                f"{cur:,} / {tot:,} 完了　"
                f"経過: {elapsed//60}分{elapsed%60}秒　"
                f"残り: {remaining//60}分{remaining%60}秒"
            )

        st.metric("現在ベストスコア", f"{best_s:.4f}")
        if best_p:
            with st.expander("現在のベストパラメータ"):
                st.json(best_p)

        # ── ペア別進捗 ────────────────────────────────────────────────────
        completed = gs_prog.get("completed_symbols", {})
        cur_sym   = gs_prog.get("current_symbol", "")
        sym_cur   = gs_prog.get("symbol_current", 0)
        sym_tot   = gs_prog.get("symbol_total",   0)
        cfg_syms  = load_config().get("symbols", [])
        if cfg_syms:
            st.markdown("**ペア別進捗**")
            for sym in cfg_syms:
                if sym in completed:
                    info   = completed[sym]
                    st_sym = info.get("status", "")
                    sc     = info.get("best_score", 0)
                    reason = info.get("reason", "")
                    if st_sym == "saved":
                        st.success(f"✅ {sym}: スコア {sc:.4f}  保存済み")
                    elif st_sym == "excluded":
                        st.error(f"❌ {sym}: 除外（{reason}）")
                    elif st_sym == "error":
                        st.warning(f"⚠️ {sym}: エラー（{reason}）")
                elif sym == cur_sym and sym_tot > 0:
                    pct = sym_cur / sym_tot
                    st.info(f"🔄 {sym}: 処理中  {sym_cur:,} / {sym_tot:,}  ({pct:.0%})")
                else:
                    st.info(f"⏳ {sym}: 待機中")

        if logs:
            st.text_area("実行ログ（最新20件）",
                         value="\n".join(logs[-20:]),
                         height=250, disabled=True, key="gs_log_area")
    else:
        st.caption("`grid_search_progress.json` がありません。実行開始後に進捗が表示されます。")

    # ── スコアランキング（完了後） ─────────────────────────────────────────
    if gs_prog is not None:
        _ranking = gs_prog.get("ranking", [])
        if _ranking:
            st.markdown("#### スコアランキング（採用/除外判定）")
            _rank_rows = []
            for _r in _ranking:
                _status_label = "✅ 採用" if _r.get("status") == "adopted" else "❌ 除外"
                _rank_rows.append({
                    "順位":   _r.get("rank", ""),
                    "銘柄":   _r.get("symbol", ""),
                    "スコア": f"{_r.get('score', 0):.4f}",
                    "結果":   _status_label,
                })
            st.dataframe(pd.DataFrame(_rank_rows), use_container_width=True, hide_index=True)

    # ── 検索結果テーブル ──────────────────────────────────────────────────
    if os.path.exists(GS_RESULTS_FILE):
        st.markdown("#### 検索結果（上位20件）")
        try:
            with open(GS_RESULTS_FILE, "r", encoding="utf-8") as f:
                gs_rows = json.load(f)

            df_gs = pd.DataFrame(gs_rows[:20])
            display_cols = [
                "symbol", "bb_period", "bb_std", "rsi_upper", "rsi_lower",
                "atr_sl_mult", "atr_tp_mult", "n_trades", "pf",
                "is_sharpe", "wft_sharpe", "score",
            ]
            display_cols = [c for c in display_cols if c in df_gs.columns]
            df_gs_display = df_gs[display_cols].copy()

            if not df_gs_display.empty and "score" in df_gs_display.columns:
                best_idx = df_gs_display["score"].idxmax()
                df_gs_display.insert(0, "rank", "")
                df_gs_display.loc[best_idx, "rank"] = "★"

            st.dataframe(df_gs_display, use_container_width=True)

            if gs_rows:
                best_row = gs_rows[0]
                st.markdown(f"**ベストスコア: {best_row['score']:.4f}** / 銘柄: {best_row.get('symbol','')}")

                if st.button("✅ ベストパラメータを採用", key="gs_adopt"):
                    symbol = best_row.get("symbol", "")
                    new_p  = {
                        k: best_row[k]
                        for k in ["bb_period", "bb_std", "rsi_period", "rsi_upper", "rsi_lower",
                                  "atr_period", "atr_sl_mult", "atr_tp_mult"]
                        if k in best_row
                    }
                    params_data = load_params() or {}
                    params_data.setdefault("params", {})[symbol] = new_p
                    params_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
                        json.dump(params_data, f, indent=2, ensure_ascii=False)

                    cfg_now = load_config()
                    if symbol and symbol not in cfg_now.get("symbols", []):
                        cfg_now.setdefault("symbols", []).append(symbol)
                        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                            json.dump(cfg_now, f, indent=2, ensure_ascii=False)

                    st.success(f"✅ {symbol} のパラメータを更新しました。📊タブを確認してください。")

        except Exception as e:
            st.error(f"結果ファイル読み込みエラー: {e}")
