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
from datetime import date, datetime, timezone, timedelta
from streamlit_autorefresh import st_autorefresh

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
SCHEDULER_PID_FILE   = os.path.join(BASE_DIR, "scheduler_pid.json")

JST = timezone(timedelta(hours=9))


# ── プロセス管理ユーティリティ ────────────────────────────────────────────

def _launch_detached(cmd: list[str]) -> subprocess.Popen:
    if sys.platform == "win32":
        return subprocess.Popen(
            cmd,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            cwd=BASE_DIR,
        )
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


def check_scheduler_status() -> bool:
    data = _read_pid_file(SCHEDULER_PID_FILE)
    pid = data.get("pid")
    if not pid:
        return False
    return psutil.pid_exists(int(pid))


def get_bot_status() -> bool:
    if platform.system() == "Windows":
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                if 'scheduler.py' in ' '.join(proc.info['cmdline'] or []):
                    return True
            except Exception:
                pass
        return False
    result = subprocess.run(
        ["systemctl", "is-active", "gmo-fx-bot"],
        capture_output=True, text=True
    )
    return result.stdout.strip() == "active"


def start_bot():
    if platform.system() == "Windows":
        subprocess.Popen(
            [sys.executable, "scheduler.py"],
            cwd=BASE_DIR,
            start_new_session=True,
        )
    else:
        subprocess.run(["sudo", "systemctl", "start", "gmo-fx-bot"])


def stop_bot():
    if platform.system() == "Windows":
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                if 'scheduler.py' in ' '.join(proc.info['cmdline'] or []):
                    proc.terminate()
            except Exception:
                pass
    else:
        subprocess.run(["sudo", "systemctl", "stop", "gmo-fx-bot"])


# ── ページ設定 ────────────────────────────────────────────────────────────

st.set_page_config(page_title="GMO FX Bot", layout="wide", page_icon="📡")

# ブラウザ翻訳ダイアログを抑制
st.markdown("""
<script>
    (function() {
        var h = document.documentElement;
        h.setAttribute('lang', 'ja');
        h.setAttribute('translate', 'no');
        h.classList.add('notranslate');
    })();
</script>
""", unsafe_allow_html=True)

# ダッシュボード CSS
st.markdown("""
<style>
/* レイアウト */
.main .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }

/* メトリクスカード */
[data-testid="metric-container"] {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 10px 14px;
}
[data-testid="stMetricValue"] { font-size: 1.35rem !important; font-weight: 700 !important; }
[data-testid="stMetricLabel"] { font-size: 0.72rem !important; color: #64748b !important; }

/* セクションラベル */
.sec-label {
    font-size: 0.62rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: #94a3b8;
    margin: 1.4rem 0 0.4rem;
    padding-bottom: 4px;
    border-bottom: 1px solid #f1f5f9;
}

/* ボットステータスバナー */
.bot-banner {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 0.9rem;
    font-weight: 600;
    margin-bottom: 1rem;
}
.bot-banner.on  { background: #f0fdf4; border: 1px solid #86efac; color: #15803d; }
.bot-banner.off { background: #fef2f2; border: 1px solid #fca5a5; color: #b91c1c; }
.dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.dot.on  { background: #22c55e; animation: pulse 2s ease-in-out infinite; }
.dot.off { background: #ef4444; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

/* サイドバー */
section[data-testid="stSidebar"] > div:first-child { padding-top: 1.5rem; }
</style>
""", unsafe_allow_html=True)

# ── 認証ゲート ────────────────────────────────────────────────────────────

_ALLOWED_USERS = {"tjxhq592@gmail.com"}

if DASHBOARD_AUTH_ENABLED:
    if not st.user.is_logged_in:
        st.title("GMO FX Bot")
        st.button("Googleでログイン", on_click=st.login)
        st.stop()
    if st.user.email not in _ALLOWED_USERS:
        st.error(f"アクセス権限がありません: {st.user.email}")
        st.button("ログアウト", on_click=st.logout)
        st.stop()


# ── データ読み込み ────────────────────────────────────────────────────────

def load_params() -> dict | None:
    if not os.path.exists(PARAMS_FILE):
        return None
    with open(PARAMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_results() -> dict | None:
    if not os.path.exists(RESULTS_FILE):
        return None
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_config() -> dict:
    default = {
        "start_date": "2024-06-16", "end_date": "auto",
        "wf_train_months": 12, "wf_test_months": 1,
        "symbols": ["AUD_NZD"], "active_symbols": ["AUD_NZD"],
        "bb_period": {"min": 10, "max": 30, "step": 5},
        "bb_std": [1.0, 1.5, 2.0, 2.5],
        "rsi_upper": {"min": 60, "max": 75, "step": 5},
        "rsi_lower": {"min": 25, "max": 40, "step": 5},
        "atr_sl_mult": [1.5, 2.0], "atr_tp_mult": [2.0, 2.5],
        "min_trades": 200, "min_pf": 1.2, "min_wft_sharpe": 0.0,
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
        if "weights" in data and "score_weights" not in data:
            data["score_weights"] = data.pop("weights")
        return {**default, **data}
    except Exception:
        return default

def _load_paper_data() -> tuple[dict, list]:
    """paper_positions と paper_trade_log をまとめて返す"""
    positions = {}
    if os.path.exists(PAPER_POSITIONS_FILE):
        try:
            with open(PAPER_POSITIONS_FILE, encoding="utf-8") as f:
                positions = json.load(f)
        except Exception:
            pass

    trades = []
    if os.path.exists(PAPER_LOG_FILE):
        try:
            with open(PAPER_LOG_FILE, encoding="utf-8") as f:
                trades = json.load(f).get("trades", [])
        except Exception:
            pass

    return positions, trades


# ── サイドバー ────────────────────────────────────────────────────────────

PAGE_OPTIONS = ["📡 ライブ", "📊 パフォーマンス", "🔧 ツール", "📋 ログ"]

_qp_page = st.query_params.get("page", "0")
try:
    _page_idx = max(0, min(int(_qp_page), len(PAGE_OPTIONS) - 1))
except (ValueError, TypeError):
    _page_idx = 0

with st.sidebar:
    st.markdown("### GMO FX Bot")
    if DASHBOARD_AUTH_ENABLED:
        st.caption(f"👤 {st.user.email}")
        if st.button("ログアウト", key="logout_btn"):
            st.logout()
    st.divider()

    page = st.radio(
        "nav",
        PAGE_OPTIONS,
        index=_page_idx,
        key="nav_page",
        label_visibility="collapsed",
    )
    st.query_params["page"] = str(PAGE_OPTIONS.index(page))

    st.divider()

    bot_running = get_bot_status()
    if bot_running:
        st.success("🟢 稼働中")
        if st.button("⏹ 停止", key="stop_bot", use_container_width=True):
            stop_bot()
            st.rerun()
    else:
        st.error("🔴 停止中")
        if st.button("▶ 起動", key="start_bot", use_container_width=True):
            start_bot()
            st.rerun()

    st.divider()
    cfg_sidebar = load_config()
    mode = "📝 ペーパー" if cfg_sidebar.get("paper_trade", True) else "🚀 本番"
    ai   = "🤖 AI判断" if cfg_sidebar.get("ai_judgment_enabled", True) else "📊 テクニカル"
    st.caption(f"{mode} · {ai}")
    active = cfg_sidebar.get("active_symbols", [])
    if active:
        st.caption(f"対象: {len(active)}ペア")


# ==================== ページ: ライブ ====================

def show_live():
    st_autorefresh(interval=5000, key="live_refresh")

    now_jst = datetime.now(JST).strftime("%H:%M:%S JST")

    # ── ステータスバナー ─────────────────────────────────────────────────
    if bot_running:
        st.markdown(
            f'<div class="bot-banner on"><span class="dot on"></span>'
            f'ボット 稼働中 <span style="margin-left:auto;font-weight:400;font-size:.8rem;color:#4ade80">'
            f'更新 {now_jst}</span></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="bot-banner off"><span class="dot off"></span>'
            f'ボット 停止中 <span style="margin-left:auto;font-weight:400;font-size:.8rem;color:#f87171">'
            f'更新 {now_jst}</span></div>',
            unsafe_allow_html=True,
        )

    positions, trades = _load_paper_data()

    # ── KPI ─────────────────────────────────────────────────────────────
    today_str   = datetime.now(JST).strftime("%Y-%m-%d")
    today_t     = [t for t in trades if t.get("datetime", "").startswith(today_str)]
    today_pnl   = sum(t["pnl"] for t in today_t)
    total_pnl   = sum(t["pnl"] for t in trades)
    wins        = [t for t in trades if t["pnl"] > 0]
    win_rate    = len(wins) / len(trades) * 100 if trades else None

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("オープン", f"{len(positions)} ポジション")
    c2.metric("本日の損益", f"¥{today_pnl:+,.0f}" if today_t else "—",
              delta=f"{len(today_t)}件" if today_t else None, delta_color="off")
    c3.metric("累積損益", f"¥{total_pnl:+,.0f}" if trades else "—",
              delta=f"{len(trades)}件" if trades else None, delta_color="off")
    c4.metric("勝率", f"{win_rate:.1f}%" if win_rate is not None else "—",
              delta=f"{len(wins)}勝 / {len(trades)-len(wins)}敗" if trades else None,
              delta_color="off")

    # ── オープンポジション ────────────────────────────────────────────────
    st.markdown('<p class="sec-label">オープンポジション</p>', unsafe_allow_html=True)

    if positions:
        try:
            from config import GMO_API_KEY, GMO_SECRET_KEY
            from gmo_client import GmoFxClient
            _gmo = GmoFxClient(GMO_API_KEY, GMO_SECRET_KEY)
        except Exception:
            _gmo = None

        rows = []
        for symbol, pos in positions.items():
            current_price = None
            if _gmo:
                try:
                    ticker = _gmo.get_ticker(symbol)
                    current_price = (float(ticker["ask"]) + float(ticker["bid"])) / 2
                except Exception:
                    pass

            if current_price is not None:
                raw_pnl = (current_price - pos["entry_price"]) * pos["size"] * 1000
                if pos["side"] == "SELL":
                    raw_pnl = -raw_pnl
                pnl_str = f"¥{raw_pnl:+,.0f}"
            else:
                pnl_str = "—"

            holding_min = ""
            if pos.get("entry_time"):
                try:
                    et = datetime.fromisoformat(pos["entry_time"])
                    diff = datetime.now() - et.replace(tzinfo=None)
                    h, m = divmod(int(diff.total_seconds() // 60), 60)
                    holding_min = f"{h}h{m:02d}m" if h else f"{m}m"
                except Exception:
                    pass

            rows.append({
                "銘柄":     symbol,
                "方向":     pos["side"],
                "建値":     f"{pos['entry_price']:.5f}",
                "現在値":   f"{current_price:.5f}" if current_price else "—",
                "含み損益": pnl_str,
                "保有時間": holding_min,
                "SL":       f"{pos.get('sl', '—')}",
                "TP":       f"{pos.get('tp', '—')}",
            })

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("現在ポジションなし")

    # ── 直近の取引 ────────────────────────────────────────────────────────
    st.markdown('<p class="sec-label">直近の取引（最新10件）</p>', unsafe_allow_html=True)

    if trades:
        recent = list(reversed(trades))[:10]
        rows = []
        for t in recent:
            pnl = t.get("pnl", 0)
            rows.append({
                "日時":   t.get("datetime", "")[:16],
                "銘柄":   t.get("symbol", ""),
                "方向":   t.get("side", ""),
                "建値":   t.get("entry_price"),
                "決済値": t.get("exit_price"),
                "損益":   f"¥{pnl:+,.0f}",
                "理由":   t.get("reason", ""),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("取引履歴なし")


# ==================== ページ: パフォーマンス ====================

def show_performance():

    params_data  = load_params()
    results_data = load_results()
    _, trades    = _load_paper_data()

    # ── サマリーメトリクス ────────────────────────────────────────────────
    st.markdown('<p class="sec-label">採用パラメータ サマリー</p>', unsafe_allow_html=True)

    if params_data:
        params      = params_data.get("params", {})
        excluded    = params_data.get("excluded", [])
        updated_at  = params_data.get("updated_at", "—")

        wft_sharpes, max_dds = [], []
        if results_data:
            for v in results_data.values():
                wft = v.get("wft") or {}
                if wft.get("sharpe") is not None:
                    wft_sharpes.append(wft["sharpe"])
                if wft.get("max_dd") is not None:
                    max_dds.append(wft["max_dd"])

        avg_sharpe = f"{sum(wft_sharpes)/len(wft_sharpes):.2f}" if wft_sharpes else "—"
        avg_dd     = f"{sum(max_dds)/len(max_dds):.1f}%"        if max_dds     else "—"

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("採用ペア数",        len(params))
        c2.metric("平均シャープ(WFT)", avg_sharpe)
        c3.metric("平均最大DD",        avg_dd)
        c4.metric("除外ペア数",        len(excluded))
        c5.metric("最終更新",          updated_at)
    else:
        st.info("params.json がありません。グリッドサーチを実行してください。")

    # ── ペーパートレード P&L チャート ─────────────────────────────────────
    st.markdown('<p class="sec-label">ペーパートレード 累積損益</p>', unsafe_allow_html=True)

    if trades:
        pnls = [t["pnl"] for t in trades]
        cum  = pd.Series(pnls).cumsum()
        cum.index = [t.get("datetime", "")[:16] for t in trades]
        cum.index.name = "日時"
        cum.name = "累積損益(円)"
        # 重複インデックスを避けてリセット
        df_cum = cum.reset_index()
        df_cum.columns = ["日時", "累積損益(円)"]
        st.line_chart(df_cum.set_index("日時"), use_container_width=True, height=220)

        # ── P&L サマリー ──────────────────────────────────────────────────
        wins    = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p <= 0]
        total   = sum(pnls)
        wr      = len(wins) / len(pnls) * 100 if pnls else 0
        avg_w   = sum(wins) / len(wins)     if wins   else 0
        avg_l   = sum(losses) / len(losses) if losses else 0
        pf      = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else None

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("総取引回数", len(pnls))
        c2.metric("勝率",       f"{wr:.1f}%")
        c3.metric("累積損益",   f"¥{total:+,.0f}")
        c4.metric("平均利益 / 損失", f"¥{avg_w:+,.0f} / ¥{avg_l:+,.0f}")
        c5.metric("プロフィットファクター", f"{pf:.2f}" if pf else "—")
    else:
        st.caption("ペーパートレード履歴なし")

    # ── エクイティカーブ（バックテスト） ─────────────────────────────────
    st.markdown('<p class="sec-label">バックテスト エクイティカーブ</p>', unsafe_allow_html=True)

    if results_data:
        equity_df = pd.DataFrame()
        for sym, v in results_data.items():
            dates  = v.get("dates", [])
            equity = v.get("equity", [])
            if dates and equity and len(dates) == len(equity):
                s = pd.Series(equity, index=pd.to_datetime(dates), name=sym)
                s = s[~s.index.duplicated(keep="first")]
                equity_df = pd.concat([equity_df, s], axis=1)
                equity_df = equity_df[~equity_df.index.duplicated(keep="first")]

        if not equity_df.empty:
            st.line_chart(equity_df, use_container_width=True, height=240)
        else:
            st.caption("エクイティデータなし")

        # WFT メトリクス テーブル
        st.markdown('<p class="sec-label">銘柄別 WFTメトリクス</p>', unsafe_allow_html=True)
        rows = []
        for sym, v in results_data.items():
            wft = v.get("wft") or {}
            rows.append({
                "銘柄":          sym,
                "シャープ(WFT)": f"{wft.get('sharpe', '—'):.2f}" if wft.get("sharpe") is not None else "—",
                "最大DD":        f"{wft.get('max_dd', '—'):.1f}%" if wft.get("max_dd") is not None else "—",
                "PF":            f"{wft.get('pf', '—'):.2f}"     if wft.get("pf") is not None else "—",
                "取引回数":      wft.get("trades", "—"),
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("backtest_results.json なし。バックテストを実行してください。")

    # ── 採用パラメータ一覧 ────────────────────────────────────────────────
    st.markdown('<p class="sec-label">採用パラメータ一覧</p>', unsafe_allow_html=True)

    if params_data and params_data.get("params"):
        rows = []
        for sym, p in params_data["params"].items():
            rows.append({"銘柄": sym, **p})
        st.dataframe(pd.DataFrame(rows).set_index("銘柄"), use_container_width=True)

        if params_data.get("excluded"):
            with st.expander(f"除外ペア ({len(params_data['excluded'])}件)"):
                for s in params_data["excluded"]:
                    st.caption(f"• {s}")
    else:
        st.caption("採用済みパラメータなし")


# ==================== ページ: ツール ====================

def show_tools():
    cfg = load_config()

    # 実行中ジョブがあればポーリング
    bt_pid_data = _read_pid_file(BT_PID_FILE)
    gs_pid_data = _read_pid_file(GS_PID_FILE)
    bt_alive    = bool(bt_pid_data.get("pid") and psutil.pid_exists(int(bt_pid_data.get("pid", 0))))
    gs_alive    = bool(gs_pid_data.get("pid") and psutil.pid_exists(int(gs_pid_data.get("pid", 0))))
    bt_running  = (bt_pid_data.get("status") == "running") and bt_alive
    gs_running  = (gs_pid_data.get("status") == "running") and gs_alive

    if bt_running or gs_running:
        st_autorefresh(interval=3000, key="tools_refresh")

    tool_tabs = st.tabs(["⚙️ 設定", "🚀 バックテスト", "🔍 グリッドサーチ"])

    # ══════════════════════════════════════════════════
    # ⚙️ 設定
    # ══════════════════════════════════════════════════
    with tool_tabs[0]:
        gs_cfg   = load_gs_config()
        sw       = gs_cfg.get("score_weights", {})
        _cpu_max = max(2, (os.cpu_count() or 1))

        _available = cfg.get("available_symbols", [
            "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY",
            "NZD_JPY", "CAD_JPY", "CHF_JPY", "ZAR_JPY",
            "EUR_USD", "GBP_USD", "AUD_USD", "EUR_GBP",
            "AUD_NZD", "EUR_CHF", "GBP_CHF", "EUR_AUD",
        ])
        _params_now   = load_params() or {}
        _adopted_syms = list(_params_now.get("params", {}).keys())
        _active_syms  = cfg.get("active_symbols", cfg.get("symbols", []))
        _gs_syms      = cfg.get("grid_search_symbols", [])

        # ── 共通設定 ─────────────────────────────────────────────────────
        st.markdown("#### 📌 共通設定")

        paper_trade = st.toggle(
            "ペーパートレードモード",
            value=cfg.get("paper_trade", True),
            help="OFFにすると実際の発注が行われます",
        )
        if paper_trade:
            st.success("📝 ペーパートレードモード：実際の発注は行われません")
        else:
            st.error("⚠️ 本番トレードモード：実際の発注が行われます")

        ai_enabled = st.toggle(
            "AI判断（Claude API）を使用する",
            value=cfg.get("ai_judgment_enabled", True),
        )

        st.markdown("#### データ期間")
        d_col1, d_col2 = st.columns(2)
        with d_col1:
            start_date_str = cfg.get("start_date", "2024-06-07")
            if start_date_str in ("auto", "") or not start_date_str:
                start_date_str = "2024-06-07"
            try:
                start_date_val = date.fromisoformat(start_date_str)
            except ValueError:
                start_date_val = date(2024, 6, 7)
            start_date = st.date_input("開始日", value=start_date_val)
        with d_col2:
            st.text_input("終了日", value="auto（昨日）", disabled=True)

        _interval_opts = ["1min", "5min", "15min", "30min", "1hour", "4hour", "1day"]
        _interval_cur  = cfg.get("interval", "30min")
        interval = st.selectbox("時間足", _interval_opts,
                                index=_interval_opts.index(_interval_cur) if _interval_cur in _interval_opts else 3)

        _ds_opts = ["gmo", "dukascopy"]
        _ds_cur  = cfg.get("data_source", "dukascopy")
        data_source = st.selectbox("データソース", _ds_opts,
                                   index=_ds_opts.index(_ds_cur) if _ds_cur in _ds_opts else 1)

        dukascopy_start_year = st.number_input(
            "データ取得開始年（Dukascopy）",
            min_value=2010, max_value=datetime.today().year - 1,
            value=int(cfg.get("dukascopy_start_year", 2016)), step=1,
            disabled=(data_source != "dukascopy"),
        )

        st.markdown("---")

        # ── グリッドサーチ設定 ────────────────────────────────────────────
        st.markdown("#### 🔍 グリッドサーチ設定")

        st.caption("グリッドサーチを実施する通貨ペア")
        gs_new_syms: list[str] = []
        gs_sym_cols = st.columns(4)
        for _i, _sym in enumerate(_available):
            with gs_sym_cols[_i % 4]:
                if st.checkbox(_sym, value=(_sym in _gs_syms), key=f"gs_sym_{_sym}"):
                    gs_new_syms.append(_sym)

        gs_top_n = st.number_input("上位N銘柄を採用", min_value=1, max_value=10,
                                    value=int(cfg.get("grid_search_top_n", 3)))

        st.markdown("**BB期間**")
        bb_col1, bb_col2, bb_col3 = st.columns(3)
        bb_min  = bb_col1.number_input("min",  value=int(cfg.get("bb_period", {}).get("min",  10)), min_value=1, step=1, key="bb_min")
        bb_max  = bb_col2.number_input("max",  value=int(cfg.get("bb_period", {}).get("max",  30)), min_value=1, step=1, key="bb_max")
        bb_step = bb_col3.number_input("step", value=int(cfg.get("bb_period", {}).get("step",  5)), min_value=1, step=1, key="bb_step")

        _bb_std_opts = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
        bb_std = st.multiselect("BB標準偏差",
                                 options=_bb_std_opts,
                                 default=[v for v in cfg.get("bb_std", [1.0, 1.5, 2.0, 2.5]) if v in _bb_std_opts])

        st.markdown("**RSI上限**")
        ru_col1, ru_col2, ru_col3 = st.columns(3)
        rsi_upper_min  = ru_col1.number_input("min",  value=int(cfg.get("rsi_upper", {}).get("min",  60)), min_value=50, max_value=95, step=5, key="ru_min")
        rsi_upper_max  = ru_col2.number_input("max",  value=int(cfg.get("rsi_upper", {}).get("max",  75)), min_value=50, max_value=95, step=5, key="ru_max")
        rsi_upper_step = ru_col3.number_input("step", value=int(cfg.get("rsi_upper", {}).get("step",  5)), min_value=1, step=1, key="ru_step")

        st.markdown("**RSI下限**")
        rl_col1, rl_col2, rl_col3 = st.columns(3)
        rsi_lower_min  = rl_col1.number_input("min",  value=int(cfg.get("rsi_lower", {}).get("min",  25)), min_value=5, max_value=50, step=5, key="rl_min")
        rsi_lower_max  = rl_col2.number_input("max",  value=int(cfg.get("rsi_lower", {}).get("max",  40)), min_value=5, max_value=50, step=5, key="rl_max")
        rsi_lower_step = rl_col3.number_input("step", value=int(cfg.get("rsi_lower", {}).get("step",  5)), min_value=1, step=1, key="rl_step")

        _atr_sl_opts = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
        atr_sl = st.multiselect("ATR損切倍率",
                                 options=_atr_sl_opts,
                                 default=[v for v in cfg.get("atr_sl_mult", [1.5, 2.0]) if v in _atr_sl_opts])

        _atr_tp_opts = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]
        atr_tp = st.multiselect("ATR利確倍率",
                                 options=_atr_tp_opts,
                                 default=[v for v in cfg.get("atr_tp_mult", [2.0, 2.5]) if v in _atr_tp_opts])

        st.markdown("**並列ワーカー数**")
        _saved_mw = max(1, min(int(gs_cfg.get("max_workers", 1)), _cpu_max))
        max_workers_val = st.slider("並列ワーカー数", min_value=1, max_value=_cpu_max,
                                     value=_saved_mw, key="gs_max_workers")

        st.markdown("**スコアリング重み**")
        wt_col1, wt_col2, wt_col3, wt_col4 = st.columns(4)
        wt_wft    = wt_col1.slider("WFTシャープ", 0.0, 1.0, float(sw.get("wft_sharpe", 0.4)), 0.05, key="wt_wft")
        wt_is     = wt_col2.slider("ISシャープ",  0.0, 1.0, float(sw.get("is_sharpe",  0.2)), 0.05, key="wt_is")
        wt_pf     = wt_col3.slider("PF",          0.0, 1.0, float(sw.get("pf",         0.2)), 0.05, key="wt_pf")
        wt_trades = wt_col4.slider("取引回数",    0.0, 1.0, float(sw.get("trades",     0.2)), 0.05, key="wt_trades")
        total_w = wt_wft + wt_is + wt_pf + wt_trades
        st.caption(f"重み合計: **{total_w:.2f}**")
        if total_w > 1.001:
            st.warning(f"⚠️ 重みの合計が {total_w:.2f} です（推奨: 1.0）")

        st.markdown("---")

        # ── バックテスト・トレード設定 ─────────────────────────────────────
        st.markdown("#### 📊 バックテスト・トレード設定")

        st.caption("グリッドサーチで採用されたペアのみ有効化できます")
        bt_new_active: list[str] = []
        bt_sym_cols = st.columns(4)
        for _i, _sym in enumerate(_available):
            with bt_sym_cols[_i % 4]:
                if _sym in _adopted_syms:
                    if st.checkbox(_sym, value=(_sym in _active_syms), key=f"bt_sym_{_sym}"):
                        bt_new_active.append(_sym)
                else:
                    st.checkbox(_sym, value=False, disabled=True, key=f"bt_sym_{_sym}")

        wf_col1, wf_col2 = st.columns(2)
        wf_train = wf_col1.slider("WFT学習期間（ヶ月）", 6, 24, value=int(cfg.get("wf_train_months", 12)))
        wf_test  = wf_col2.slider("WFT検証期間（ヶ月）", 1, 6,  value=int(cfg.get("wf_test_months", 1)))

        ex_col1, ex_col2, ex_col3 = st.columns(3)
        min_trades     = ex_col1.number_input("最小取引回数",    value=int(cfg.get("min_trades", 100)),   min_value=0)
        min_pf         = ex_col2.number_input("最小PF",         value=float(cfg.get("min_pf", 1.2)),     min_value=0.0, step=0.1, format="%.1f")
        min_wft_sharpe = ex_col3.number_input("最小WFTシャープ", value=float(cfg.get("min_wft_sharpe", -0.3)), step=0.1, format="%.1f")

        st.markdown("---")

        if st.button("💾 設定を保存", type="primary", use_container_width=True, key="save_settings"):
            errors = []
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
                "symbols":             bt_new_active,
                "bb_period":           {"min": int(bb_min), "max": int(bb_max), "step": int(bb_step)},
                "bb_std":              sorted(bb_std) if bb_std else [2.0],
                "rsi_upper":           {"min": int(rsi_upper_min), "max": int(rsi_upper_max), "step": int(rsi_upper_step)},
                "rsi_lower":           {"min": int(rsi_lower_min), "max": int(rsi_lower_max), "step": int(rsi_lower_step)},
                "atr_sl_mult":         sorted(atr_sl) if atr_sl else [1.5],
                "atr_tp_mult":         sorted(atr_tp) if atr_tp else [2.0],
                "min_trades":          int(min_trades),
                "min_pf":              float(min_pf),
                "min_wft_sharpe":      float(min_wft_sharpe),
                "max_workers":         max_workers_val,
                "paper_trade":         paper_trade,
                "ai_judgment_enabled": ai_enabled,
                "interval":            interval,
                "data_source":         data_source,
                "dukascopy_start_year": int(dukascopy_start_year),
            }
            try:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            except Exception as e:
                errors.append(f"backtest_config.json 保存エラー: {e}")

            existing_gs = load_gs_config()
            existing_gs["score_weights"] = {
                "wft_sharpe": wt_wft, "is_sharpe": wt_is, "pf": wt_pf, "trades": wt_trades,
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
                    f"✅ 保存しました — "
                    f"GS対象: {', '.join(gs_new_syms) or '(未選択)'}  /  "
                    f"アクティブ: {', '.join(bt_new_active) or '(なし)'}"
                )

    # ══════════════════════════════════════════════════
    # 🚀 バックテスト
    # ══════════════════════════════════════════════════
    with tool_tabs[1]:
        # PIDファイルの死活を再判定（tool_tabs[0]での変数を引き継がない）
        bt_pd2   = _read_pid_file(BT_PID_FILE)
        bt_pid2  = bt_pd2.get("pid")
        bt_alive2 = bool(bt_pid2 and psutil.pid_exists(int(bt_pid2)))
        bt_run2   = (bt_pd2.get("status") == "running") and bt_alive2

        if bt_run2 and not bt_alive2 and os.path.exists(BT_PID_FILE):
            bt_pd2["status"] = "stopped"
            with open(BT_PID_FILE, "w", encoding="utf-8") as f:
                json.dump(bt_pd2, f, indent=2)

        if bt_run2:
            st.info(f"⏳ バックテスト実行中  (PID: {bt_pid2}  開始: {bt_pd2.get('started_at', '')})")
            if st.button("⏹ 停止", key="bt_stop"):
                if bt_pid2 and _kill_pid(bt_pid2):
                    bt_pd2["status"] = "stopped"
                    with open(BT_PID_FILE, "w", encoding="utf-8") as f:
                        json.dump(bt_pd2, f, indent=2)
                    st.warning("バックテストを停止しました。")
                else:
                    st.error("停止に失敗しました。")
        else:
            col_btn, col_code = st.columns([1, 2])
            with col_btn:
                if st.button("▶ バックテスト開始", type="primary", key="bt_start", use_container_width=True):
                    try:
                        proc = _launch_detached([sys.executable, "backtest.py"])
                        pid_data = {
                            "pid": proc.pid,
                            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "running",
                        }
                        with open(BT_PID_FILE, "w", encoding="utf-8") as f:
                            json.dump(pid_data, f, indent=2)
                        if os.path.exists(BT_PROGRESS_FILE):
                            os.remove(BT_PROGRESS_FILE)
                        st.success(f"起動しました (PID: {proc.pid})")
                    except Exception as e:
                        st.error(f"起動失敗: {e}")
            with col_code:
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
                st.caption(f"{cur} / {tot} 銘柄完了  現在: {symbol}")

            if logs:
                st.text_area("実行ログ（最新20件）", value="\n".join(logs[-20:]),
                             height=260, disabled=True, key="bt_log_area")
        else:
            st.caption("backtest_progress.json なし。実行後に進捗が表示されます。")

    # ══════════════════════════════════════════════════
    # 🔍 グリッドサーチ
    # ══════════════════════════════════════════════════
    with tool_tabs[2]:
        gs_pd2   = _read_pid_file(GS_PID_FILE)
        gs_pid2  = gs_pd2.get("pid")
        gs_alive2 = bool(gs_pid2 and psutil.pid_exists(int(gs_pid2)))
        gs_run2   = ((gs_pd2.get("status") == "running") and gs_alive2) \
                    or st.session_state.get("gs_force_running", False)

        if gs_pd2.get("status") == "running" and not gs_alive2 and os.path.exists(GS_PID_FILE):
            gs_pd2["status"] = "stopped"
            with open(GS_PID_FILE, "w", encoding="utf-8") as f:
                json.dump(gs_pd2, f, indent=2)
            st.session_state.pop("gs_force_running", None)
        elif gs_alive2:
            st.session_state.pop("gs_force_running", None)

        if gs_run2:
            st.info(f"⏳ グリッドサーチ実行中  (PID: {gs_pid2}  開始: {gs_pd2.get('started_at', '')})")
            if st.button("⏹ 停止", key="gs_stop"):
                if gs_pid2 and _kill_pid(gs_pid2):
                    gs_pd2["status"] = "stopped"
                    with open(GS_PID_FILE, "w", encoding="utf-8") as f:
                        json.dump(gs_pd2, f, indent=2)
                    st.warning("グリッドサーチを停止しました。")
                else:
                    st.error("停止に失敗しました。")
        else:
            col_btn, col_code = st.columns([1, 2])
            with col_btn:
                if st.button("▶ 開始", type="primary", key="gs_start", use_container_width=True):
                    try:
                        proc = _launch_detached([sys.executable, "grid_search_runner.py"])
                        _init_pid = {
                            "pid": proc.pid,
                            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "running",
                        }
                        with open(GS_PID_FILE, "w", encoding="utf-8") as f:
                            json.dump(_init_pid, f, indent=2)
                        if os.path.exists(GS_PROGRESS_FILE):
                            os.remove(GS_PROGRESS_FILE)

                        for _ in range(20):
                            time.sleep(0.5)
                            if os.path.exists(GS_PID_FILE):
                                try:
                                    with open(GS_PID_FILE, "r", encoding="utf-8") as _f:
                                        _pc = json.load(_f)
                                    if _pc.get("status") == "running":
                                        st.session_state["gs_force_running"] = True
                                        break
                                except Exception:
                                    pass

                        st.success(f"起動しました (PID: {proc.pid})")
                        st.rerun()
                    except Exception as e:
                        st.error(f"起動失敗: {e}")
            with col_code:
                st.code("python grid_search_runner.py", language="bash")

        gs_prog = _read_progress_json(GS_PROGRESS_FILE)
        if gs_prog is not None:
            status    = gs_prog.get("status", "running")
            cur       = gs_prog.get("current", 0)
            tot       = gs_prog.get("total", 1)
            elapsed   = gs_prog.get("elapsed", 0)
            remaining = gs_prog.get("remaining", 0)
            best_s    = gs_prog.get("best_score", 0)
            best_p    = gs_prog.get("best_params", {})
            logs      = gs_prog.get("log", [])

            if status == "completed":
                st.success("✅ グリッドサーチ完了！params.json に保存されました。")
            elif status == "error":
                st.error("⚠️ エラーで終了しました。")
            else:
                ratio = cur / tot if tot > 0 else 0.0
                st.progress(ratio)
                st.caption(
                    f"{cur:,} / {tot:,} 完了  "
                    f"経過: {elapsed//60}分{elapsed%60}秒  "
                    f"残り: {remaining//60}分{remaining%60}秒"
                )

            c1, c2 = st.columns(2)
            c1.metric("ベストスコア", f"{best_s:.4f}")
            if best_p:
                with c2.expander("ベストパラメータ"):
                    st.json(best_p)

            # ── ペア別進捗 ────────────────────────────────────────────────
            completed    = gs_prog.get("completed_symbols", {})
            sym_progress = gs_prog.get("symbol_progress", {})
            _cfg_now     = load_config()
            _gs_syms2    = _cfg_now.get("grid_search_symbols") or _cfg_now.get("symbols", [])
            _all_syms    = list(dict.fromkeys(list(_gs_syms2) + list(completed.keys())))

            if _all_syms:
                st.markdown('<p class="sec-label">ペア別進捗</p>', unsafe_allow_html=True)
                for sym in _all_syms:
                    if sym in completed:
                        info   = completed[sym]
                        st_sym = info.get("status", "")
                        sc     = info.get("best_score", 0)
                        reason = info.get("reason", "")
                        if st_sym == "saved":
                            st.success(f"✅ {sym}  スコア {sc:.4f}  採用")
                        elif st_sym == "excluded":
                            st.error(f"❌ {sym}  除外（{reason}）")
                        elif st_sym == "error":
                            st.warning(f"⚠️ {sym}  エラー（{reason}）")
                        else:
                            st.info(f"🔄 {sym}  スコア {sc:.4f}")
                    elif sym in sym_progress:
                        prog  = sym_progress[sym]
                        s_st  = prog.get("status", "waiting")
                        cur_c = prog.get("current", 0)
                        tot_c = prog.get("total", 1)
                        bsc   = prog.get("best_score", 0.0)
                        pct   = prog.get("pct", 0.0)
                        if s_st == "running":
                            st.info(f"🔄 {sym}  {cur_c:,}/{tot_c:,}件  ({pct:.1f}%)  ベスト={bsc:.4f}")
                        elif s_st == "completed":
                            st.success(f"✅ {sym}  ベスト={bsc:.4f}")
                        else:
                            st.info(f"⏳ {sym}  待機中")
                    else:
                        st.info(f"⏳ {sym}  待機中")

            if logs:
                st.text_area("実行ログ（最新20件）", value="\n".join(logs[-20:]),
                             height=220, disabled=True, key="gs_log_area")

            # スコアランキング
            _ranking = gs_prog.get("ranking", [])
            if _ranking:
                st.markdown('<p class="sec-label">スコアランキング</p>', unsafe_allow_html=True)
                rank_rows = []
                for _r in _ranking:
                    rank_rows.append({
                        "順位":   _r.get("rank", ""),
                        "銘柄":   _r.get("symbol", ""),
                        "スコア": f"{_r.get('score', 0):.4f}",
                        "結果":   "✅ 採用" if _r.get("status") == "adopted" else "❌ 除外",
                    })
                st.dataframe(pd.DataFrame(rank_rows), use_container_width=True, hide_index=True)

        else:
            st.caption("grid_search_progress.json なし。実行後に進捗が表示されます。")

        # 採用パラメータ
        st.markdown('<p class="sec-label">採用パラメータ</p>', unsafe_allow_html=True)
        try:
            if os.path.exists(PARAMS_FILE):
                with open(PARAMS_FILE, encoding="utf-8") as f:
                    pd_gs = json.load(f)
            else:
                pd_gs = {}

            adopted = pd_gs.get("params", {})
            if adopted:
                rows = []
                for symbol, p in adopted.items():
                    rows.append({
                        "銘柄": symbol,
                        "bb_period": p.get("bb_period"),
                        "bb_std": p.get("bb_std"),
                        "rsi_upper": p.get("rsi_upper"),
                        "rsi_lower": p.get("rsi_lower"),
                        "atr_sl_mult": p.get("atr_sl_mult"),
                        "atr_tp_mult": p.get("atr_tp_mult"),
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                st.caption("採用済みパラメータなし")
        except Exception as e:
            st.error(f"params.json 読み込みエラー: {e}")


# ==================== ページ: ログ ====================

def show_log():
    st_autorefresh(interval=8000, key="log_refresh")

    col_filter, col_dl = st.columns([3, 1])
    with col_filter:
        log_filter = st.selectbox(
            "フィルタ",
            ["すべて", "ERRORのみ", "WARNINGのみ", "Claudeの判断のみ", "Polymarketのみ", "経済指標のみ"],
            label_visibility="collapsed",
        )

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

    def _match(line: str) -> bool:
        if log_filter == "すべて":          return True
        if log_filter == "ERRORのみ":       return "ERROR" in line
        if log_filter == "WARNINGのみ":     return "WARNING" in line
        if log_filter == "Claudeの判断のみ": return "Claude" in line
        if log_filter == "Polymarketのみ":  return "Polymarket" in line or "polymarket" in line
        if log_filter == "経済指標のみ":    return "経済指標" in line
        return True

    filtered = [ln.rstrip() for ln in raw_lines if ln.strip() and _match(ln)]
    display_lines = list(reversed(filtered))[:200]

    if not display_lines:
        st.info("ログが見つかりません。")
        return

    st.caption(f"{len(display_lines)} 行表示（最新順）")

    for line in display_lines:
        if "ERROR" in line:
            st.error(line)
        elif "WARNING" in line:
            st.warning(line)
        elif "Claude" in line:
            st.info(line)
        elif "Polymarket" in line or "polymarket" in line or "経済指標" in line:
            st.success(line)
        else:
            st.text(line)


# ==================== ルーティング ====================

if page == "📡 ライブ":
    show_live()
elif page == "📊 パフォーマンス":
    show_performance()
elif page == "🔧 ツール":
    show_tools()
elif page == "📋 ログ":
    show_log()
