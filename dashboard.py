# 起動: streamlit run dashboard.py

import streamlit as st
import pandas as pd
import re
import json
import os

PARAMS_FILE = "params.json"
RESULTS_FILE = "backtest_results.json"
LOG_FILE = "trade_log.txt"

st.set_page_config(page_title="GMO FX Bot Dashboard", layout="wide")
st.title("GMO FX Bot ダッシュボード")


# ==================== データ読込 ====================

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


def load_log():
    """
    FX形式:  "買い注文実行: USD_JPY 0.1lot @ 150.25"
              "決済注文実行: USD_JPY 0.1lot"
    株形式(後方互換): "買い注文実行: AAPL 10株 @ $150.25"
                      "売り注文実行: MSFT 1株"
    """
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
                    records.append({
                        "日時": m.group(1),
                        "通貨ペア": m.group(2),
                        "種別": "買い",
                        "数量": float(m.group(3)),
                        "価格": float(m.group(4)),
                    })
                    continue
                m = pat_sell.search(line)
                if m:
                    records.append({
                        "日時": m.group(1),
                        "通貨ペア": m.group(2),
                        "種別": "決済",
                        "数量": float(m.group(3)),
                        "価格": None,
                    })
    except Exception as e:
        st.warning(f"ログ読込エラー: {e}")

    return pd.DataFrame(records)


# ==================== 1. サマリーカード ====================

st.subheader("サマリー")
params_data = load_params()

if params_data is None:
    st.warning(f"`{PARAMS_FILE}` が見つかりません。`python backtest.py` を実行してください。")
else:
    params    = params_data.get("params", {})
    excluded  = params_data.get("excluded", [])
    updated_at = params_data.get("updated_at", "—")

    results_data = load_results()
    wft_sharpes, max_dds = [], []
    if results_data:
        for v in results_data.values():
            if v.get("wft_sharpe") is not None:
                wft_sharpes.append(v["wft_sharpe"])
            if v.get("max_dd") is not None:
                max_dds.append(v["max_dd"])

    avg_sharpe = f"{sum(wft_sharpes)/len(wft_sharpes):.2f}" if wft_sharpes else "—"
    avg_dd     = f"{sum(max_dds)/len(max_dds):.1f}%"         if max_dds     else "—"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("対象ペア数",        len(params))
    c2.metric("平均シャープ(WFT)", avg_sharpe)
    c3.metric("平均最大DD",        avg_dd)
    c4.metric("除外ペア数",        len(excluded))
    c5.metric("最終更新",          updated_at)

# ==================== 2. バックテスト結果テーブル ====================

st.subheader("最適パラメータ一覧")
if params_data and params_data.get("params"):
    rows = [
        {
            "通貨ペア":        sym,
            "ma_short":        p.get("ma_short"),
            "ma_long":         p.get("ma_long"),
            "rsi_upper":       p.get("rsi_upper"),
            "rsi_lower":       p.get("rsi_lower"),
            "stop_loss_pct":   p.get("stop_loss_pct"),
            "take_profit_pct": p.get("take_profit_pct"),
        }
        for sym, p in params_data["params"].items()
    ]
    st.dataframe(
        pd.DataFrame(rows).set_index("通貨ペア"),
        use_container_width=True,
    )
    if excluded:
        with st.expander("除外ペア"):
            for s in excluded:
                st.write(f"- {s}")
else:
    st.info("params.json にパラメータがありません。")

# ==================== 3. エクイティカーブ ====================

st.subheader("エクイティカーブ")
results_data = load_results()
if results_data is None:
    st.info(
        f"`{RESULTS_FILE}` が見つかりません。"
        " `python backtest.py` を実行するとエクイティカーブが保存されます。"
    )
else:
    equity_df = pd.DataFrame()
    for sym, v in results_data.items():
        dates  = v.get("dates", [])
        equity = v.get("equity", [])
        if dates and equity and len(dates) == len(equity):
            s = pd.Series(equity, index=pd.to_datetime(dates), name=sym)
            equity_df = pd.concat([equity_df, s], axis=1)

    if equity_df.empty:
        st.info("エクイティデータが空です。")
    else:
        st.line_chart(equity_df)

# ==================== 4. 取引履歴 ====================

st.subheader("取引履歴")
df_log = load_log()
if df_log.empty:
    st.info(
        f"`{LOG_FILE}` に取引記録がありません。"
        " ボットを実行すると取引ログが蓄積されます。"
    )
else:
    st.dataframe(df_log, use_container_width=True)

    c1, c2 = st.columns(2)
    c1.metric("買い注文 合計", int((df_log["種別"] == "買い").sum()))
    c2.metric("決済注文 合計", int((df_log["種別"] == "決済").sum()))

    st.markdown("**通貨ペア別 取引回数**")
    count_df = (
        df_log.groupby(["通貨ペア", "種別"])
        .size()
        .reset_index(name="回数")
        .pivot(index="通貨ペア", columns="種別", values="回数")
        .fillna(0)
        .astype(int)
    )
    st.bar_chart(count_df)
