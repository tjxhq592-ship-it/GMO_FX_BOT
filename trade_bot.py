"""
GMOコイン 外国為替FX 自動取引ボット
元ソース: Alpaca 株式取引ボット (trade_bot.py) を移植
"""
import json
import logging
import re

import anthropic
import requests

from polymarket import get_polymarket_signal
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


# ── Polymarketコンテキスト生成 ──────────────────────────────────────────
def build_polymarket_context(signal: dict) -> str:
    if not signal.get("enabled"):
        return "（対象マーケットなし）"
    lines = []
    for m in signal.get("markets", []):
        surge_mark = " ⚠️急変中" if m["surge"] else ""
        lines.append(f"  - {m['question']}: Yes={m['prob']:.0%}{surge_mark}")
    if not lines:
        return "（取得データなし）"
    return "\n".join(lines)


def should_block_entry(signal: dict) -> bool:
    return signal.get("risk_block", False) or signal.get("surge_detected", False)


# ── Claudeに判断を依頼 ─────────────────────────────────────────────────
def ask_claude(bars, symbol: str, symbol_params: dict, poly_signal: dict) -> dict:
    p      = symbol_params[symbol]
    latest = bars.iloc[-1]
    prev   = bars.iloc[-2]

    recent = bars.iloc[-5:]
    recent_summary = "\n".join([
        f"  {row.name.date() if hasattr(row.name, 'date') else ''} "
        f"終値:{row['close']:.5f}  RSI:{row['RSI']:.1f}  出来高:{int(row['volume']):,}"
        for _, row in recent.iterrows()
    ])

    poly_context = build_polymarket_context(poly_signal)

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

【Polymarketマクロ環境】
{poly_context}

ルール：
- リスクを極力抑えた小額取引を重視
- RSIが{p['rsi_upper']}以上は買いを避ける
- RSIが{p['rsi_lower']}以下は売りを避ける
- EMA{p['ma_short']}がEMA{p['ma_long']}を上抜けたら買いシグナル
- EMA{p['ma_short']}がEMA{p['ma_long']}を下抜けたら売りシグナル
- MACD・BB・直近トレンド・出来高変化も考慮すること
- Polymarketで急変・高確率イベントがある場合はリスクを強く意識すること
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
            bars       = get_market_data(symbol, symbol_params)
            position   = get_position(symbol)
            poly_signal = get_polymarket_signal(symbol)

            # 市場環境（通貨ペア自身のトレンドで判断）
            market = get_market_condition(bars)
            logging.info(f"市場環境({symbol}): {market}")
            logging.info(f"Polymarket({symbol}): risk_block={poly_signal['risk_block']}  surge={poly_signal['surge_detected']}")

            # ポジション保有中に急変検知 → 即決済
            if position and poly_signal.get("surge_detected"):
                close_side = "SELL" if position["side"] == "BUY" else "BUY"
                gmo.close_position(position["positionId"], symbol, close_side, int(position["size"]))
                msg = f"⚡ Polymarket急変検知のため即決済\n{symbol} @ {float(bars.iloc[-1]['close']):.5f}"
                logging.info(msg)
                send_line(msg)
                sell_count += 1
                summary_lines.append(f"  {symbol}: Polymarket急変決済")
                continue

            logging.info("Claudeに判断を依頼中...")
            decision = ask_claude(bars, symbol, symbol_params, poly_signal)
            logging.info(f"Claudeの判断: {decision['action']} - {decision['reason']}")

            price = float(bars.iloc[-1]["close"])
            size  = calc_trade_size(cash, price)

            logging.info(f"{symbol} 価格: {price}, 取引数量: {size}ロット, ポジション: {position}")

            # ATRベースのSL/TP算出（ロング・ショートで上下反転）
            atr         = float(bars.iloc[-1]["ATR"])
            atr_sl_mult = p.get("atr_sl_mult", 1.5)
            atr_tp_mult = p.get("atr_tp_mult", 2.5)
            long_sl  = round(price - atr * atr_sl_mult, 5)
            long_tp  = round(price + atr * atr_tp_mult, 5)
            short_sl = round(price + atr * atr_sl_mult, 5)
            short_tp = round(price - atr * atr_tp_mult, 5)

            pos_side = position["side"] if position else None

            # ── 新規ロング ──────────────────────────────────────────────
            if decision["action"] == "buy" and position is None and market == "bull":
                if should_block_entry(poly_signal):
                    logging.info(f"{symbol} Polymarketリスクブロック: ロングスキップ")
                    summary_lines.append(f"  {symbol}: Polymarketリスクブロックでスキップ")
                    continue

                gmo.place_order(symbol, "BUY", size)
                logging.info(f"買い注文実行: {symbol} {size}ロット @ {price}  SL={long_sl}  TP={long_tp}")
                send_line(
                    f"📈 新規ロング\n通貨ペア: {symbol}\n価格: {price:.5f}\n"
                    f"数量: {size}ロット\nATR: {atr:.5f}\n"
                    f"利確: {long_tp:.5f}  損切: {long_sl:.5f}\n理由: {decision['reason']}"
                )
                buy_count += 1
                summary_lines.append(f"  {symbol}: ロング {size}ロット @ {price:.5f}")

            # ── 新規ショート ────────────────────────────────────────────
            elif decision["action"] == "sell" and position is None and market == "bear":
                if should_block_entry(poly_signal):
                    logging.info(f"{symbol} Polymarketリスクブロック: ショートスキップ")
                    summary_lines.append(f"  {symbol}: Polymarketリスクブロックでスキップ")
                    continue

                gmo.place_order(symbol, "SELL", size)
                logging.info(f"新規ショート: {symbol} {size}ロット @ {price}  SL={short_sl}  TP={short_tp}")
                send_line(
                    f"📉 新規ショート\n通貨ペア: {symbol}\n価格: {price:.5f}\n"
                    f"数量: {size}ロット\nATR: {atr:.5f}\n"
                    f"利確: {short_tp:.5f}  損切: {short_sl:.5f}\n理由: {decision['reason']}"
                )
                sell_count += 1
                summary_lines.append(f"  {symbol}: ショート {size}ロット @ {price:.5f}")

            # ── ロング決済（Claudeがsell判断 かつ ロング保有中）──────────
            elif decision["action"] == "sell" and pos_side == "BUY":
                gmo.close_position(position["positionId"], symbol, "SELL", int(position["size"]))
                logging.info(f"ロング決済: {symbol} {position['size']}ロット @ {price:.5f}")
                send_line(
                    f"✅ ロング決済\n通貨ペア: {symbol}\n価格: {price:.5f}\n理由: {decision['reason']}"
                )
                sell_count += 1
                summary_lines.append(f"  {symbol}: ロング決済 @ {price:.5f}")

            # ── ショート決済（Claudeがbuy判断 かつ ショート保有中）────────
            elif decision["action"] == "buy" and pos_side == "SELL":
                gmo.close_position(position["positionId"], symbol, "BUY", int(position["size"]))
                logging.info(f"ショート決済: {symbol} {position['size']}ロット @ {price:.5f}")
                send_line(
                    f"✅ ショート決済\n通貨ペア: {symbol}\n価格: {price:.5f}\n理由: {decision['reason']}"
                )
                buy_count += 1
                summary_lines.append(f"  {symbol}: ショート決済 @ {price:.5f}")

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
