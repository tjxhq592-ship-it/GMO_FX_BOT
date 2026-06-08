# 起動: streamlit run dashboard.py

import platform
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
from config import DASHBOARD_AUTH_ENABLED
from datetime import date, datetime
from streamlit_autorefresh import st_autorefresh

# スクリプトと同じディレクトリを基準ディレクトリとして使用（相対パス問題を回避）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

PARAMS_FILE          = os.path.join(BASE_DIR, "params.json")
RESULTS_FILE         = os.path.join(BASE_DIR, "backtest_results.json")
LOG_FILE             = os.path.join(BASE_DIR, "trade_log.txt")
CONFIG_FILE          = os.path.join(BASE_DIR, "backtest_config.json")
GS_PROGRESS_FILE     = os.path.join(BASE_DIR, "grid_search_progress.json")
GS_RESULTS_FILE      = os.path.join(BASE_DIR, "grid_search_results.json")
GS_CONFIG_FILE       = os.path.join(BASE_DIR, "grid_search_config.json")
GS_PID_FILE          = os.path.join(BASE_DIR, "grid_search_pid.json")
BT_PROGRESS_FILE     = os.path.join(BASE_DIR, "backtest_progress.json")
BT_PID_FILE          = os.path.join(BASE_DIR, "backtest_pid.json")
PAPER_POSITIONS_FILE = os.path.join(BASE_DIR, "paper_positions.json")
PAPER_LOG_FILE       = os.path.join(BASE_DIR, "paper_trade_log.json")


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


SCHEDULER_PID_FILE = os.path.join(BASE_DIR, "scheduler_pid.json")


def check_scheduler_status() -> bool:
    data = _read_pid_file(SCHEDULER_PID_FILE)
    pid = data.get("pid")
    if not pid:
        return False
    return psutil.pid_exists(int(pid))


def get_bot_status() -> bool:
    """ボットの稼働状態を確認（Windows: psutil / Linux: systemctl）"""
    if platform.system() == "Windows":
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                if 'scheduler.py' in ' '.join(proc.info['cmdline'] or []):
                    return True
            except Exception:
                pass
        return False
    else:
        result = subprocess.run(
            ["systemctl", "is-active", "gmo-fx-bot"],
            capture_output=True, text=True
        )
        return result.stdout.strip() == "active"


def start_bot():
    """ボットを起動（Windows: subprocess.Popen / Linux: systemctl）"""
    if platform.system() == "Windows":
        subprocess.Popen(
            [sys.executable, "scheduler.py"],
            cwd=BASE_DIR,
            start_new_session=True,
        )
    else:
        subprocess.run(["sudo", "systemctl", "start", "gmo-fx-bot"])


def stop_bot():
    """ボットを停止（Windows: psutil.terminate / Linux: systemctl）"""
    if platform.system() == "Windows":
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                if 'scheduler.py' in ' '.join(proc.info['cmdline'] or []):
                    proc.terminate()
            except Exception:
                pass
    else:
        subprocess.run(["sudo", "systemctl", "stop", "gmo-fx-bot"])


st.set_page_config(page_title="GMO FX Bot Dashboard", layout="wide")

# ===== 認証ゲート =====
_ALLOWED_USERS = {"tjxhq592@gmail.com"}

if DASHBOARD_AUTH_ENABLED:
    if not st.user.is_logged_in:
        st.title("GMO FX Bot ダッシュボード")
        st.button("Googleでログイン", on_click=st.login)
        st.stop()

    if st.user.email not in _ALLOWED_USERS:
        st.error(f"アクセス権限がありません: {st.user.email}")
        st.button("ログアウト", on_click=st.logout)
        st.stop()
# ===== 認証ゲートここまで =====


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


# ==================== サイドバーナビゲーション ====================

with st.sidebar:
    st.title("GMO FX Bot")
    if DASHBOARD_AUTH_ENABLED:
        st.markdown(f"👤 {st.user.email}")
        if st.button("ログアウト", key="logout_btn"):
            st.logout()
    st.divider()

    page = st.radio(
        "メニュー",
        ["📊 ダッシュボード",
         "⚙️ 設定",
         "🚀 バックテスト実行",
         "🔍 グリッドサーチ",
         "📝 ペーパートレード",
         "📋 ログ"],
        label_visibility="collapsed",
    )

    st.divider()

    if get_bot_status():
        st.success("🟢 ボット稼働中")
        if st.button("⏹ ボット停止", key="stop_bot", use_container_width=True):
            stop_bot()
            st.rerun()
    else:
        st.error("🔴 ボット停止中")
        if st.button("▶ ボット起動", key="start_bot", use_container_width=True):
            start_bot()
            st.rerun()


# ==================== ページ: ダッシュボード ====================

def show_dashboard():

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



# ==================== ページ: 設定 ====================

def show_settings():
    st.subheader("設定")
    cfg      = load_config()
    gs_cfg   = load_gs_config()
    sw       = gs_cfg.get("score_weights", {})
    _cpu_max = max(2, (os.cpu_count() or 1))

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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 📌 共通設定
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    st.markdown("### 📌 共通設定")

    # ── ペーパートレードモード ──────────────────────────────────────────
    paper_trade = st.toggle(
        "ペーパートレードモード",
        value=cfg.get("paper_trade", True),
        help="OFFにすると実際の発注が行われます",
    )
    if paper_trade:
        st.success("📝 ペーパートレードモード：実際の発注は行われません")
    else:
        st.error("⚠️ 本番トレードモード：実際の発注が行われます")

    st.markdown("#### データ期間")
    d_col1, d_col2 = st.columns(2)
    with d_col1:
        start_date_str = cfg.get("start_date", "2024-06-07")
        if start_date_str == "auto" or not start_date_str:
            start_date_str = "2024-06-07"
        try:
            start_date_val = date.fromisoformat(start_date_str)
        except ValueError:
            start_date_val = date(2024, 6, 7)
        start_date = st.date_input(
            "開始日",
            value=start_date_val,
        )
    with d_col2:
        st.text_input("終了日", value="auto（昨日）", disabled=True)

    _interval_options = ["1min", "5min", "15min", "30min", "1hour", "4hour", "1day"]
    _interval_current = cfg.get("interval", "30min")
    _interval_idx     = _interval_options.index(_interval_current) if _interval_current in _interval_options else 3
    interval = st.selectbox(
        "時間足",
        options=_interval_options,
        index=_interval_idx,
        help="バックテスト・グリッドサーチで使用する時間足",
    )

    _ds_options = ["gmo", "dukascopy"]
    _ds_current = cfg.get("data_source", "dukascopy")
    _ds_idx     = _ds_options.index(_ds_current) if _ds_current in _ds_options else 1
    data_source = st.selectbox(
        "データソース",
        options=_ds_options,
        index=_ds_idx,
        help="gmo: 直近2年（API取得） / dukascopy: 10年分（バックテスト推奨）",
    )

    dukascopy_start_year = st.number_input(
        "データ取得開始年（Dukascopy）",
        min_value=2010,
        max_value=datetime.today().year - 1,
        value=int(cfg.get("dukascopy_start_year", 2016)),
        step=1,
        disabled=(data_source != "dukascopy"),
        help="data_source=dukascopy のときのみ有効",
    )

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

    _bb_std_opts = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
    _bb_std_def  = [v for v in cfg.get("bb_std", [1.0, 1.5, 2.0, 2.5]) if v in _bb_std_opts]
    bb_std = st.multiselect("BB標準偏差 (bb_std)", options=_bb_std_opts, default=_bb_std_def)

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

    _atr_sl_opts = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    _atr_sl_def  = [v for v in cfg.get("atr_sl_mult", [1.5, 2.0]) if v in _atr_sl_opts]
    atr_sl = st.multiselect("ATR損切倍率 (atr_sl_mult)", options=_atr_sl_opts, default=_atr_sl_def)

    _atr_tp_opts = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]
    _atr_tp_def  = [v for v in cfg.get("atr_tp_mult", [2.0, 2.5]) if v in _atr_tp_opts]
    atr_tp = st.multiselect("ATR利確倍率 (atr_tp_mult)", options=_atr_tp_opts, default=_atr_tp_def)

    st.markdown("#### 並列ワーカー数")
    _saved_mw = min(int(gs_cfg.get("max_workers", 1)), _cpu_max)
    _saved_mw = max(1, _saved_mw)
    max_workers_val = st.slider(
        "並列ワーカー数",
        min_value=1,
        max_value=_cpu_max,
        value=_saved_mw,
        help="大きいほど速いがPC負荷が上がる（グリッドサーチはローカルPCで実行推奨）",
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
    if st.button("💾 設定を保存", type="primary", use_container_width=True, key="save_settings"):
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
            "max_workers":           max_workers_val,
            "paper_trade":           paper_trade,
            "interval":              interval,
            "data_source":           data_source,
            "dukascopy_start_year":  int(dukascopy_start_year),
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



# ==================== ページ: バックテスト実行 ====================

def show_backtest():
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



# ==================== ページ: グリッドサーチ ====================

def show_grid_search():
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
            st.success("✅ グリッドサーチ完了！パラメータは自動的にparams.jsonに保存されました。バックテストタブで結果を確認してください。")
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
        completed      = gs_prog.get("completed_symbols", {})
        sym_progress   = gs_prog.get("symbol_progress", {})

        # 表示対象: grid_search_symbols（設定値）∪ completed_symbols のキー
        _cfg_now  = load_config()
        _gs_syms  = _cfg_now.get("grid_search_symbols") or _cfg_now.get("symbols", [])
        _all_syms = list(dict.fromkeys(list(_gs_syms) + list(completed.keys())))

        if _all_syms:
            st.markdown("**ペア別進捗**")
            for sym in _all_syms:
                # completed_symbols に最終ステータスがある場合を優先
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
                    elif st_sym == "pending":
                        st.info(f"🔄 {sym}: 処理完了（採用判定中）スコア {sc:.4f}")
                    else:
                        st.info(f"⏳ {sym}: 待機中")
                # symbol_progress にリアルタイム進捗がある場合
                elif sym in sym_progress:
                    prog   = sym_progress[sym]
                    s_stat = prog.get("status", "waiting")
                    cur_c  = prog.get("current", 0)
                    tot_c  = prog.get("total", 1)
                    bsc    = prog.get("best_score", 0.0)
                    pct    = prog.get("pct", 0.0)
                    if s_stat == "running":
                        st.info(f"🔄 {sym}: {cur_c:,} / {tot_c:,} 件処理中  ({pct:.1f}%)  ベスト={bsc:.4f}")
                    elif s_stat == "completed":
                        st.success(f"✅ {sym}: 完了  ベスト={bsc:.4f}")
                    else:
                        st.info(f"⏳ {sym}: 待機中")
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

    # ── 採用パラメータテーブル ────────────────────────────────────────────
    st.subheader("✅ 採用パラメータ")
    try:
        if os.path.exists(PARAMS_FILE):
            with open(PARAMS_FILE, encoding="utf-8") as f:
                params_data = json.load(f)
        else:
            params_data = {}

        adopted = params_data.get("params", {})
        if adopted:
            rows = []
            for symbol, p in adopted.items():
                rows.append({
                    "銘柄":       symbol,
                    "bb_period":  p.get("bb_period"),
                    "bb_std":     p.get("bb_std"),
                    "rsi_upper":  p.get("rsi_upper"),
                    "rsi_lower":  p.get("rsi_lower"),
                    "atr_sl_mult": p.get("atr_sl_mult"),
                    "atr_tp_mult": p.get("atr_tp_mult"),
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True)
        else:
            st.info("採用済みパラメータがありません。グリッドサーチを実行してください。")

        excluded = params_data.get("excluded", [])
        if excluded:
            with st.expander(f"❌ 除外ペア ({len(excluded)}件)"):
                for e in excluded:
                    st.write(f"• {e}")

    except Exception as e:
        st.error(f"params.json 読み込みエラー: {e}")


# ==================== ページ: ペーパートレード ====================

def show_paper_trade():
    st.subheader("📝 ペーパートレード")
    st_autorefresh(interval=3000, key="paper_refresh")

    # ── GMOクライアント（リアルタイム価格取得用）────────────────────────
    try:
        from config import GMO_API_KEY, GMO_SECRET_KEY
        from gmo_client import GmoFxClient
        _gmo = GmoFxClient(GMO_API_KEY, GMO_SECRET_KEY)
    except Exception:
        _gmo = None

    # ── 現在のポジション ─────────────────────────────────────────────────
    st.markdown("### 現在のポジション")
    try:
        if os.path.exists(PAPER_POSITIONS_FILE):
            with open(PAPER_POSITIONS_FILE, encoding="utf-8") as f:
                positions = json.load(f)
        else:
            positions = {}

        if positions:
            pos_rows = []
            for symbol, pos in positions.items():
                # リアルタイム価格取得
                current_price = None
                if _gmo:
                    try:
                        ticker = _gmo.get_ticker(symbol)
                        current_price = (float(ticker["ask"]) + float(ticker["bid"])) / 2
                    except Exception:
                        pass

                if current_price is not None:
                    pnl = (current_price - pos["entry_price"]) * pos["size"]
                    if pos["side"] == "SELL":
                        pnl = -pnl
                    pnl_str = f"{pnl:+.0f}円"
                else:
                    current_price = None
                    pnl_str = "取得失敗"

                pos_rows.append({
                    "銘柄":       symbol,
                    "サイド":     pos["side"],
                    "数量":       pos["size"],
                    "建値":       pos["entry_price"],
                    "現在価格":   f"{current_price:.5f}" if current_price else "—",
                    "含み損益":   pnl_str,
                    "SL":         pos.get("sl", "—"),
                    "TP":         pos.get("tp", "—"),
                    "建玉時刻":   pos.get("entry_time", "—")[:19],
                })

            df_pos = pd.DataFrame(pos_rows)
            st.dataframe(df_pos, use_container_width=True, hide_index=True)
        else:
            st.info("現在ポジションなし")

    except Exception as e:
        st.error(f"ポジション読み込みエラー: {e}")

    st.divider()

    # ── 取引履歴 & 累積損益 ──────────────────────────────────────────────
    st.markdown("### 累積損益サマリー")
    try:
        if os.path.exists(PAPER_LOG_FILE):
            with open(PAPER_LOG_FILE, encoding="utf-8") as f:
                log_data = json.load(f)
            trades = log_data.get("trades", [])
        else:
            trades = []

        if trades:
            pnls      = [t["pnl"] for t in trades]
            wins      = [p for p in pnls if p > 0]
            losses    = [p for p in pnls if p <= 0]
            win_rate  = len(wins) / len(pnls) * 100 if pnls else 0
            total_pnl = sum(pnls)
            max_win   = max(pnls) if pnls else 0
            max_loss  = min(pnls) if pnls else 0

            c1, c2, c3, c4, c5, c6 = st.columns(6)
            c1.metric("総取引回数",  len(trades))
            c2.metric("勝ち",        len(wins))
            c3.metric("負け",        len(losses))
            c4.metric("勝率",        f"{win_rate:.1f}%")
            c5.metric("総損益",      f"{total_pnl:+.0f}円")
            c6.metric("最大利益 / 最大損失",
                       f"{max_win:+.0f} / {max_loss:+.0f}円")
        else:
            st.info("取引履歴なし")

    except Exception as e:
        st.error(f"ログ読み込みエラー: {e}")
        trades = []

    st.divider()

    # ── 取引履歴テーブル ─────────────────────────────────────────────────
    st.markdown("### 取引履歴")
    if trades:
        df_log = pd.DataFrame([
            {
                "日時":     t.get("datetime", "")[:19],
                "銘柄":     t.get("symbol", ""),
                "サイド":   t.get("side", ""),
                "建値":     t.get("entry_price"),
                "決済価格": t.get("exit_price"),
                "損益(円)": t.get("pnl"),
                "理由":     t.get("reason", ""),
            }
            for t in reversed(trades)   # 新しい順
        ])
        st.dataframe(df_log, use_container_width=True, hide_index=True)
    else:
        st.info("取引履歴がありません。グリッドサーチ後にボットを起動してください。")


# ==================== ページ: ログ ====================

def show_log():
    st.subheader("📋 トレードボットログ")
    st_autorefresh(interval=3000, key="log_refresh")

    # ── フィルタ & ダウンロードボタン ────────────────────────────────────
    col_filter, col_dl = st.columns([3, 1])
    with col_filter:
        log_filter = st.selectbox(
            "フィルタ",
            ["すべて", "ERRORのみ", "WARNINGのみ", "Claudeの判断のみ", "Polymarketのみ"],
            label_visibility="collapsed",
        )

    # ── ログ読み込み ─────────────────────────────────────────────────────
    raw_lines: list[str] = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                raw_lines = f.readlines()
        except Exception as e:
            st.error(f"ログ読み込みエラー: {e}")

    with col_dl:
        st.download_button(
            label="⬇ ダウンロード",
            data="".join(raw_lines).encode("utf-8"),
            file_name="trade_log.txt",
            mime="text/plain",
            use_container_width=True,
        )

    # ── フィルタリング ────────────────────────────────────────────────────
    def _match(line: str) -> bool:
        if log_filter == "すべて":
            return True
        if log_filter == "ERRORのみ":
            return "ERROR" in line
        if log_filter == "WARNINGのみ":
            return "WARNING" in line
        if log_filter == "Claudeの判断のみ":
            return "Claude" in line
        if log_filter == "Polymarketのみ":
            return "Polymarket" in line or "polymarket" in line
        return True

    filtered = [ln.rstrip() for ln in raw_lines if ln.strip() and _match(ln)]

    # 新しい順に最大200行表示
    display_lines = list(reversed(filtered))[:200]

    if not display_lines:
        st.info("ログが見つかりません。")
        return

    st.caption(f"表示: {len(display_lines)} 行（最新順）")

    # ── 色分け表示 ────────────────────────────────────────────────────────
    for line in display_lines:
        if "ERROR" in line:
            st.error(line)
        elif "WARNING" in line:
            st.warning(line)
        elif "Claude" in line:
            st.info(line)
        elif "Polymarket" in line or "polymarket" in line:
            st.success(line)
        else:
            st.text(line)


# ==================== ページルーティング ====================

if page == "📊 ダッシュボード":
    show_dashboard()
elif page == "⚙️ 設定":
    show_settings()
elif page == "🚀 バックテスト実行":
    show_backtest()
elif page == "🔍 グリッドサーチ":
    show_grid_search()
elif page == "📝 ペーパートレード":
    show_paper_trade()
elif page == "📋 ログ":
    show_log()
