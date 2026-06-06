"""
GMOコイン 外国為替FX 自動取引ボット
元ソース: Alpaca 株式取引ボット (trade_bot.py) を移植
"""
import json
import logging
import re

import anthropic
import requests

from config import (
    GMO_API_KEY, GMO_SECRET_KEY, ANTHROPIC_API_KEY,
    LOG_FILE, PARAMS_FILE, SYMBOLS,
    LINE_CHANNEL_TOKEN, LINE_USER_ID,
)
from gmo_client import GmoFxClient
from utils import (
    calculate_rsi, calculate_macd, calculate_bollinger, calculate_atr,
    get_market_condition, calc_trade_size,
)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)

# ── LINE通知 ────────────────────────────────────────────────────────────
def send_line(message: str) -> None:
    try:
        url     = "https://api.line.me/v2/bot/message/push"
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
        }
        data = {
            "to":       LINE_USER_ID,
            "messages": [{"type": "text", "text": message}],
        }
        r = requests.post(url, headers=headers, json=data, timeout=10)
        if r.status_code != 200:
            logging.error(f"LINE送信失敗: {r.status_code} {r.text}")
    except Exception as e:
        logging.error(f"LINE送信エラー: {e}")


# ── パラメータ読み込み ────────────────────────────────────────────────────
def load_params() -> dict:
    with open(PARAMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["params"]


# ── クライアント初期化 ────────────────────────────────────────────────────
gmo    = GmoFxClient(GMO_API_KEY, GMO_SECRET_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── 市場データ取得 ────────────────────────────────────────────────────────
def get_market_data(symbol: str, symbol_params: dict) -> object:
    p    = symbol_params[symbol]
    bars = gmo.get_klines_range(symbol, interval="1day", days=90)

    bars["MA_short"] = bars["close"].ewm(span=p["ma_short"], adjust=False).mean()
    bars["MA_long"]  = bars["close"].ewm(span=p["ma_long"],  adjust=False).mean()
    bars["RSI"]      = calculate_rsi(bars["close"])

    macd = calculate_macd(bars["close"])
    bars["MACD_hist"] = macd["hist"]

    bb = calculate_bollinger(bars["close"])
    bars["BB_mid"] = bb["mid"]

    atr_period    = p.get("atr_period", 14)
    bars["ATR"]   = calculate_atr(bars["high"], bars["low"], bars["close"], period=atr_period)

    return bars


# ── Claudeに判断を依頼 ─────────────────────────────────────────────────
def ask_claude(bars, symbol: str, symbol_params: dict) -> dict:
    p      = symbol_params[symbol]
    latest = bars.iloc[-1]
    prev   = bars.iloc[-2]

    recent = bars.iloc[-5:]
    recent_summary = "\n".join([
        f"  {row.name.date() if hasattr(row.name, 'date') else ''} "
        f"終値:{row['close']:.5f}  RSI:{row['RSI']:.1f}  出来高:{int(row['volume']):,}"
        for _, row in recent.iterrows()
    ])

    prompt = f"""
あなたはFXトレードAIです。以下のデータを分析して売買判断をしてください。

通貨ペア: {symbol}
現在値: {latest['close']:.5f}
EMA{p['ma_short']}: {latest['MA_short']:.5f}
EMA{p['ma_long']}: {latest['MA_long']:.5f}
RSI: {latest['RSI']:.1f}
MACD_hist: {latest['MACD_hist']:.6f}
BB_mid: {latest['BB_mid']:.5f}
前日EMA{p['ma_short']}: {prev['MA_short']:.5f}
前日EMA{p['ma_long']}: {prev['MA_long']:.5f}

【直近5日間の推移】
{recent_summary}

ルール：
- リスクを極力抑えた小額取引を重視
- RSIが{p['rsi_upper']}以上は買いを避ける
- RSIが{p['rsi_lower']}以下は売りを避ける
- EMA{p['ma_short']}がEMA{p['ma_long']}を上抜けたら買いシグナル
- EMA{p['ma_short']}がEMA{p['ma_long']}を下抜けたら売りシグナル
- MACD・BB・直近トレンド・出来高変化も考慮すること
- FXはレバレッジがあるため特にリスク管理を優先すること

以下のJSON形式のみで回答してください（他の文章は不要）：
{{"action": "buy" または "sell" または "hold", "reason": "理由を日本語で簡潔に"}}
"""

    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    response = message.content[0].text
    logging.info(f"Claudeの返答({symbol}): {response}")

    match = re.search(r'\{.*?\}', response, re.DOTALL)
    if match:
        return json.loads(match.group())
    logging.warning(f"JSON取得失敗({symbol})、holdとして処理")
    return {"action": "hold", "reason": "判断取得失敗"}


# ── ポジション確認 ────────────────────────────────────────────────────────
def get_position(symbol: str) -> dict | None:
    positions = gmo.get_open_positions(symbol)
    return positions[0] if positions else None


# ── メインループ ──────────────────────────────────────────────────────────
def run_bot() -> None:
    logging.info("=== GMO FXボット起動 ===")

    cash = gmo.get_cash_jpy()
    logging.info(f"有効証拠金: ¥{cash:,.0f}")
    send_line(f"🤖 GMO FXボット起動\n有効証拠金: ¥{cash:,.0f}\n対象ペア: {', '.join(SYMBOLS)}")

    symbol_params = load_params()
    logging.info(f"パラメータ読み込み完了: {list(symbol_params.keys())}")

    summary_lines: list[str] = []
    buy_count  = 0
    sell_count = 0

    for symbol in SYMBOLS:
        if symbol not in symbol_params:
            logging.warning(f"{symbol} のパラメータが params.json に存在しません。スキップします。")
            continue

        logging.info(f"--- {symbol} ---")
        p = symbol_params[symbol]

        try:
            bars     = get_market_data(symbol, symbol_params)
            position = get_position(symbol)

            # 市場環境（通貨ペア自身のトレンドで判断）
            market = get_market_condition(bars)
            logging.info(f"市場環境({symbol}): {market}")

            logging.info("Claudeに判断を依頼中...")
            decision = ask_claude(bars, symbol, symbol_params)
            logging.info(f"Claudeの判断: {decision['action']} - {decision['reason']}")

            price = float(bars.iloc[-1]["close"])
            size  = calc_trade_size(cash, price)  # ロット数（1000通貨単位）

            logging.info(f"{symbol} 価格: {price}, 取引数量: {size}ロット, ポジション: {position}")

            # ATRベースのSL/TP算出
            atr          = float(bars.iloc[-1]["ATR"])
            atr_sl_mult  = p.get("atr_sl_mult", 1.5)
            atr_tp_mult  = p.get("atr_tp_mult", 2.5)
            sl           = round(price - atr * atr_sl_mult, 5)
            tp           = round(price + atr * atr_tp_mult, 5)

            if decision["action"] == "buy" and position is None and market == "bull":
                result = gmo.place_order(symbol, "BUY", size)
                logging.info(f"買い注文実行: {symbol} {size}ロット @ {price}  SL={sl}  TP={tp}")

                msg = (
                    f"📈 買い注文実行\n"
                    f"通貨ペア: {symbol}\n"
                    f"価格: {price:.5f}\n"
                    f"数量: {size}ロット\n"
                    f"ATR: {atr:.5f}\n"
                    f"利確ライン: {tp:.5f}\n"
                    f"損切ライン: {sl:.5f}\n"
                    f"理由: {decision['reason']}"
                )
                send_line(msg)
                buy_count += 1
                summary_lines.append(f"  {symbol}: 買い {size}ロット @ {price:.5f}  SL={sl}  TP={tp}")

                # SL/TP監視: 現在価格がラインを超えたら即決済
                current_price = float(gmo.get_klines_range(symbol, interval="1min", days=1).iloc[-1]["close"])
                if current_price <= sl or current_price >= tp:
                    pos = get_position(symbol)
                    if pos:
                        close_side = "SELL" if pos["side"] == "BUY" else "BUY"
                        gmo.close_position(pos["positionId"], symbol, close_side, int(pos["size"]))
                        trigger = "TP到達" if current_price >= tp else "SL到達"
                        logging.info(f"即決済({trigger}): {symbol} @ {current_price:.5f}")
                        send_line(f"🔔 {trigger}で即決済\n{symbol} @ {current_price:.5f}")

            elif decision["action"] == "sell" and position is not None:
                pos_id   = position["positionId"]
                pos_size = int(position["size"])
                # 建玉の逆サイドで決済
                close_side = "SELL" if position["side"] == "BUY" else "BUY"
                gmo.close_position(pos_id, symbol, close_side, pos_size)
                logging.info(f"決済注文実行: {symbol} {pos_size}ロット")

                msg = (
                    f"📉 決済注文実行\n"
                    f"通貨ペア: {symbol}\n"
                    f"価格: {price:.5f}\n"
                    f"数量: {pos_size}ロット\n"
                    f"理由: {decision['reason']}"
                )
                send_line(msg)
                sell_count += 1
                summary_lines.append(f"  {symbol}: 決済 {pos_size}ロット @ {price:.5f}")

            else:
                logging.info(f"{symbol} 待機中")
                summary_lines.append(f"  {symbol}: 待機")

        except Exception as e:
            error_msg = f"⚠️ エラー発生\n通貨ペア: {symbol}\n内容: {str(e)}"
            logging.error(error_msg)
            send_line(error_msg)

    # サマリー通知
    cash = gmo.get_cash_jpy()
    summary = (
        f"📊 本日のサマリー\n"
        f"有効証拠金: ¥{cash:,.0f}\n"
        f"買い注文: {buy_count}件\n"
        f"決済注文: {sell_count}件\n"
        f"取引詳細:\n" + "\n".join(summary_lines)
    )
    send_line(summary)
    logging.info("=== ボット終了 ===")


if __name__ == "__main__":
    run_bot()
