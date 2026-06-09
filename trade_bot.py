"""
GMOコイン 外国為替FX 自動取引ボット
元ソース: Alpaca 株式取引ボット (trade_bot.py) を移植
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta

import anthropic
import requests

from polymarket import get_polymarket_signal
from config import (
    GMO_API_KEY, GMO_SECRET_KEY, ANTHROPIC_API_KEY,
    LOG_FILE, PARAMS_FILE, SYMBOLS,
)
from gmo_client import GmoFxClient
from notifier import send_telegram
from utils import (
    calculate_rsi, calculate_bollinger, calculate_atr,
    get_market_condition, calc_trade_size,
)
from fileops import atomic_write_json, locked_read_json, locked_update_json
from logger_config import configure_logging

JST = timezone(timedelta(hours=9))


BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
BT_CONFIG_FILE       = os.path.join(BASE_DIR, "backtest_config.json")
PAPER_POSITIONS_FILE = os.path.join(BASE_DIR, "paper_positions.json")
PAPER_LOG_FILE       = os.path.join(BASE_DIR, "paper_trade_log.json")


# ── 設定読み込み ──────────────────────────────────────────────────────────
def _load_bt_config() -> dict:
    try:
        with open(BT_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

_bt_cfg        = _load_bt_config()
PAPER_TRADE    = _bt_cfg.get("paper_trade", True)
TRADE_INTERVAL = _bt_cfg.get("interval", "30min")


# ── パラメータ読み込み ────────────────────────────────────────────────────
def load_params() -> dict:
    with open(PARAMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["params"]


# ── クライアント初期化 ────────────────────────────────────────────────────
gmo    = GmoFxClient(GMO_API_KEY, GMO_SECRET_KEY, notify_fn=send_telegram)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── ペーパーポジション管理 ────────────────────────────────────────────────
def _load_paper_positions() -> dict:
    return locked_read_json(PAPER_POSITIONS_FILE, default={})

def _save_paper_positions(positions: dict) -> None:
    atomic_write_json(PAPER_POSITIONS_FILE, positions)

def load_paper_position(symbol: str) -> dict | None:
    return _load_paper_positions().get(symbol)

def place_order_paper(
    symbol: str,
    side: str,
    size: int,
    entry_price: float,
    sl: float,
    tp: float,
) -> None:
    """ペーパーポジションを paper_positions.json に記録して通知。"""
    def _updater(curr: dict) -> dict:
        if curr is None:
            curr = {}
        curr[symbol] = {
            "side":        side,
            "size":        size,
            "entry_price": entry_price,
            "entry_time":  datetime.now().isoformat(),
            "sl":          sl,
            "tp":          tp,
        }
        return curr

    locked_update_json(PAPER_POSITIONS_FILE, _updater, default={})
    send_telegram(
        f"📝[PAPER] {side}注文\n"
        f"銘柄: {symbol}\n"
        f"価格: {entry_price}\n"
        f"数量: {size}通貨\n"
        f"SL: {sl}\n"
        f"TP: {tp}"
    )

def _append_paper_log(trade: dict) -> None:
    """paper_trade_log.json に取引結果を追記する。"""
    try:
        def _updater(curr: dict) -> dict:
            if curr is None:
                curr = {"trades": []}
            curr.setdefault("trades", []).append(trade)
            return curr

        locked_update_json(PAPER_LOG_FILE, _updater, default={"trades": []})
    except Exception as e:
        logging.error(f"paper_trade_log.json 書き込みエラー: {e}")


def close_position_paper(symbol: str, exit_price: float, reason: str) -> None:
    """ペーパーポジションを決済して損益を通知し、ログに記録する。"""
    pos = load_paper_position(symbol)
    if not pos:
        return
    pnl = (exit_price - pos["entry_price"]) * pos["size"] * 1000
    if pos["side"] == "SELL":
        pnl = -pnl
    send_telegram(
        f"📝[PAPER] 決済\n"
        f"銘柄: {symbol}\n"
        f"決済価格: {exit_price}\n"
        f"理由: {reason}\n"
        f"損益: {pnl:+.0f}円"
    )
    # ログ追記
    _append_paper_log({
        "datetime":    datetime.now().isoformat(),
        "symbol":      symbol,
        "side":        pos["side"],
        "size":        pos["size"],
        "entry_price": pos["entry_price"],
        "exit_price":  exit_price,
        "pnl":         round(pnl, 2),
        "reason":      reason,
    })
    def _remover(curr: dict) -> dict:
        if not curr:
            return {}
        curr.pop(symbol, None)
        return curr

    locked_update_json(PAPER_POSITIONS_FILE, _remover, default={})


# ── 市場データ取得 ────────────────────────────────────────────────────────
def get_market_data(symbol: str, symbol_params: dict) -> object:
    p    = symbol_params[symbol]
    bars = gmo.get_klines_bulk(symbol, interval=TRADE_INTERVAL, years=1)

    bb_period = p.get("bb_period", 20)
    bb_std    = p.get("bb_std", 2.0)
    bb = calculate_bollinger(bars["close"], period=bb_period, std_mult=bb_std)
    bars["BB_upper"] = bb["upper"]
    bars["BB_mid"]   = bb["mid"]
    bars["BB_lower"] = bb["lower"]

    rsi_period  = p.get("rsi_period", 14)
    bars["RSI"] = calculate_rsi(bars["close"], period=rsi_period)

    atr_period  = p.get("atr_period", 14)
    bars["ATR"] = calculate_atr(bars["high"], bars["low"], bars["close"], period=atr_period)

    bars["ATR_avg20"] = bars["ATR"].rolling(20).mean()

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


# ── Claude API 失敗時のフォールバック判断 ────────────────────────────────
def ask_claude_fallback(bars, symbol: str, symbol_params: dict) -> dict:
    """Claude API 失敗時のルールベースフォールバック判断"""
    p      = symbol_params[symbol]
    latest = bars.iloc[-1]

    price    = float(latest["close"])
    bb_upper = float(latest["BB_upper"])
    bb_lower = float(latest["BB_lower"])
    rsi      = float(latest["RSI"])

    if price < bb_lower and rsi <= p["rsi_lower"]:
        return {"action": "buy",  "reason": "[FALLBACK] BB下限+RSI売られすぎ"}
    elif price > bb_upper and rsi >= p["rsi_upper"]:
        return {"action": "sell", "reason": "[FALLBACK] BB上限+RSI買われすぎ"}
    else:
        return {"action": "hold", "reason": "[FALLBACK] Claude API失敗のため待機"}


# ── Claudeに判断を依頼 ─────────────────────────────────────────────────
def ask_claude(bars, symbol: str, symbol_params: dict, poly_signal: dict) -> dict:
    import pandas as pd
    p      = symbol_params[symbol]
    latest = bars.iloc[-1]

    recent = bars.iloc[-5:]
    recent_summary = "\n".join([
        f"  {row.name.date() if hasattr(row.name, 'date') else ''} "
        f"終値:{row['close']:.5f}  RSI:{row['RSI']:.1f}"
        f"  BB上:{row['BB_upper']:.5f} / 下:{row['BB_lower']:.5f}"
        for _, row in recent.iterrows()
    ])

    poly_context = build_polymarket_context(poly_signal)

    atr_now     = latest["ATR"]
    atr_avg     = latest["ATR_avg20"]
    atr_avg_str = f"{atr_avg:.5f}" if pd.notna(atr_avg) else "N/A"
    in_range    = (atr_now < atr_avg * p.get("atr_range_mult", 1.0)) if pd.notna(atr_avg) else False
    range_str   = "レンジ相場" if in_range else "トレンド相場"

    prompt = f"""
あなたはFXトレードAIです。ボリンジャーバンド+RSI逆張り戦略でレンジ相場の売買判断をしてください。

通貨ペア: {symbol}
現在値: {latest['close']:.5f}
BB上限: {latest['BB_upper']:.5f}
BB中心: {latest['BB_mid']:.5f}
BB下限: {latest['BB_lower']:.5f}
RSI({p.get('rsi_period', 14)}): {latest['RSI']:.1f}
ATR: {atr_now:.5f} / ATR20期間平均: {atr_avg_str}
相場環境: {range_str}

【直近5日間の推移】
{recent_summary}

【Polymarketマクロ環境】
{poly_context}

ルール：
- レンジ相場（ATRが平均以下）でのみエントリーを推奨
- 終値がBB下限を下回り RSI≦{p['rsi_lower']}なら買いシグナル
- 終値がBB上限を上回り RSI≧{p['rsi_upper']}なら売りシグナル
- 終値がBB中心付近なら決済を検討
- トレンド相場ではholdを優先すること
- Polymarketで急変・高確率イベントがある場合はholdすること
- FXはレバレッジがあるため特にリスク管理を優先すること

以下のJSON形式のみで回答してください（他の文章は不要）：
{{"action": "buy" または "sell" または "hold", "reason": "理由を日本語で簡潔に"}}
"""

    max_attempts = 3
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
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

        except anthropic.AuthenticationError as e:
            # 401: 認証エラー → リトライ不要、即フォールバック
            msg = f"⚠️ Claude API 認証エラー（401）: {e}"
            logging.error(msg)
            send_telegram(msg)
            return ask_claude_fallback(bars, symbol, symbol_params)

        except anthropic.RateLimitError as e:
            # 429: レート制限 → 60秒待機してリトライ
            logging.warning(f"Claude API レート制限（429）attempt={attempt}: {e}")
            last_error = e
            if attempt < max_attempts:
                time.sleep(60)
                continue
            # 3回失敗
            break

        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", 0)
            # クレジット切れ (529 / 402 など) → 即フォールバック
            if status in (402, 529) or "credit" in str(e).lower():
                msg = f"⚠️ Claude API クレジット不足: {e}"
                logging.error(msg)
                send_telegram(msg)
                return ask_claude_fallback(bars, symbol, symbol_params)
            # 500系サーバーエラー → 2秒待機してリトライ
            logging.warning(f"Claude API サーバーエラー（{status}）attempt={attempt}: {e}")
            last_error = e
            if attempt < max_attempts:
                time.sleep(2)
                continue
            break

        except anthropic.APIError as e:
            # その他 API エラー → 2秒待機してリトライ
            logging.warning(f"Claude API エラー attempt={attempt}: {e}")
            last_error = e
            if attempt < max_attempts:
                time.sleep(2)
                continue
            break

        except Exception as e:
            logging.error(f"ask_claude 予期しないエラー({symbol}): {e}")
            last_error = e
            break

    # 全リトライ失敗 → フォールバック
    msg = f"⚠️ Claude API失敗 フォールバックモードで動作中\n最終エラー: {last_error}"
    logging.error(msg)
    send_telegram(msg)
    return ask_claude_fallback(bars, symbol, symbol_params)


# ── ポジション確認 ────────────────────────────────────────────────────────
def get_position(symbol: str) -> dict | None:
    if PAPER_TRADE:
        return load_paper_position(symbol)
    positions = gmo.get_open_positions(symbol)
    return positions[0] if positions else None


# ── 高スプレッド時間帯チェック ────────────────────────────────────────────
def _is_high_spread_period() -> bool:
    now = datetime.now(JST)
    h = now.hour
    return 3 <= h < 9


# ── 期限切れ指値注文のキャンセル ──────────────────────────────────────────
def _cancel_stale_orders(symbol: str) -> None:
    if PAPER_TRADE:
        return
    try:
        orders  = gmo.get_active_orders(symbol)
        now_ms  = int(time.time() * 1000)
        for order in orders:
            order_ts = int(order.get("timestamp", now_ms))
            age_h    = (now_ms - order_ts) / 3_600_000
            if age_h >= 1:
                order_id = order.get("orderId", "")
                gmo.cancel_order(order_id)
                logging.info(f"期限切れ指値注文キャンセル: {symbol} orderId={order_id}")
    except Exception as e:
        logging.warning(f"注文キャンセルチェックエラー ({symbol}): {e}")


# ── メインループ ──────────────────────────────────────────────────────────
def run_bot() -> None:
    _cfg = _load_bt_config()
    ai_judgment_enabled = _cfg.get("ai_judgment_enabled", True)
    current_symbols = _cfg.get("active_symbols", _cfg.get("symbols", SYMBOLS))

    mode_label = "📝ペーパートレード" if PAPER_TRADE else "🚀本番トレード"
    logging.info(f"=== GMO FXボット起動 [{mode_label}] ===")

    if PAPER_TRADE:
        cash_str = "（ペーパー）"
    else:
        cash     = gmo.get_cash_jpy()
        cash_str = f"¥{cash:,.0f}"

    send_telegram(
        f"🤖 GMO FXボット起動 [{mode_label}]\n"
        f"有効証拠金: {cash_str}\n"
        f"対象ペア: {', '.join(current_symbols)}"
    )

    symbol_params = load_params()
    logging.info(f"パラメータ読み込み完了: {list(symbol_params.keys())}")

    summary_lines: list[str] = []
    buy_count  = 0
    sell_count = 0

    for symbol in current_symbols:
        if symbol not in symbol_params:
            logging.warning(f"{symbol} のパラメータが params.json に存在しません。スキップします。")
            continue

        logging.info(f"--- {symbol} ---")
        p = symbol_params[symbol]

        try:
            _cancel_stale_orders(symbol)

            bars        = get_market_data(symbol, symbol_params)
            position    = get_position(symbol)
            poly_signal = get_polymarket_signal(symbol)

            market = get_market_condition(bars)
            logging.info(f"市場環境({symbol}): {market}")
            logging.info(f"Polymarket({symbol}): risk_block={poly_signal['risk_block']}  surge={poly_signal['surge_detected']}")

            # ポジション保有中に急変検知 → 即決済
            if position and poly_signal.get("surge_detected"):
                exit_price = float(bars.iloc[-1]["close"])
                if PAPER_TRADE:
                    close_position_paper(symbol, exit_price, "Polymarket急変検知")
                else:
                    close_side = "SELL" if position["side"] == "BUY" else "BUY"
                    gmo.close_position(position["positionId"], symbol, close_side, int(position["size"]))
                    msg = f"⚡ Polymarket急変検知のため即決済\n{symbol} @ {exit_price:.5f}"
                    logging.info(msg)
                    send_telegram(msg)
                sell_count += 1
                summary_lines.append(f"  {symbol}: Polymarket急変決済")
                continue

            if ai_judgment_enabled:
                logging.info("Claudeに判断を依頼中...")
                decision = ask_claude(bars, symbol, symbol_params, poly_signal)
                logging.info(f"Claudeの判断: {decision['action']} - {decision['reason']}")
            else:
                decision = ask_claude_fallback(bars, symbol, symbol_params)
                logging.info(f"AI判断OFF: テクニカルのみで判断 → {decision['action']} - {decision['reason']}")

            price = float(bars.iloc[-1]["close"])
            size  = calc_trade_size(gmo.get_cash_jpy() if not PAPER_TRADE else 1_000_000, price)

            atr         = float(bars.iloc[-1]["ATR"])
            atr_sl_mult = p.get("atr_sl_mult", 1.5)
            atr_tp_mult = p.get("atr_tp_mult", 2.5)

            long_limit  = round(float(bars.iloc[-1]["BB_lower"]), 5)
            short_limit = round(float(bars.iloc[-1]["BB_upper"]), 5)

            long_sl  = round(long_limit - atr * atr_sl_mult, 5)
            long_tp  = round(long_limit + atr * atr_tp_mult, 5)
            short_sl = round(short_limit + atr * atr_sl_mult, 5)
            short_tp = round(short_limit - atr * atr_tp_mult, 5)

            pos_side = position["side"] if position else None

            # ── 新規ロング ─────────────────────────────────────────────
            if decision["action"] == "buy" and position is None:
                if should_block_entry(poly_signal):
                    logging.info(f"{symbol} Polymarketリスクブロック: ロングスキップ")
                    summary_lines.append(f"  {symbol}: Polymarketリスクブロックでスキップ")
                    continue
                if _is_high_spread_period():
                    logging.info(f"{symbol} 高スプレッド時間帯: ロングエントリーをスキップ")
                    summary_lines.append(f"  {symbol}: 高スプレッド時間帯スキップ")
                    continue

                if PAPER_TRADE:
                    place_order_paper(symbol, "BUY", size, long_limit, long_sl, long_tp)
                else:
                    gmo.place_order(symbol, "BUY", size, order_type="LIMIT", price=long_limit)
                    send_telegram(
                        f"📈 指値ロング発注\n通貨ペア: {symbol}\n指値: {long_limit:.5f}\n"
                        f"数量: {size}ロット\nATR: {atr:.5f}\n"
                        f"利確: {long_tp:.5f}  損切: {long_sl:.5f}\n理由: {decision['reason']}"
                    )
                logging.info(f"ロング発注: {symbol} {size}ロット @ {long_limit}  SL={long_sl}  TP={long_tp}")
                buy_count += 1
                summary_lines.append(f"  {symbol}: {'[PAPER]' if PAPER_TRADE else ''}指値ロング @ {long_limit:.5f}")

            # ── 新規ショート ────────────────────────────────────────────
            elif decision["action"] == "sell" and position is None:
                if should_block_entry(poly_signal):
                    logging.info(f"{symbol} Polymarketリスクブロック: ショートスキップ")
                    summary_lines.append(f"  {symbol}: Polymarketリスクブロックでスキップ")
                    continue
                if _is_high_spread_period():
                    logging.info(f"{symbol} 高スプレッド時間帯: ショートエントリーをスキップ")
                    summary_lines.append(f"  {symbol}: 高スプレッド時間帯スキップ")
                    continue

                if PAPER_TRADE:
                    place_order_paper(symbol, "SELL", size, short_limit, short_sl, short_tp)
                else:
                    gmo.place_order(symbol, "SELL", size, order_type="LIMIT", price=short_limit)
                    send_telegram(
                        f"📉 指値ショート発注\n通貨ペア: {symbol}\n指値: {short_limit:.5f}\n"
                        f"数量: {size}ロット\nATR: {atr:.5f}\n"
                        f"利確: {short_tp:.5f}  損切: {short_sl:.5f}\n理由: {decision['reason']}"
                    )
                logging.info(f"ショート発注: {symbol} {size}ロット @ {short_limit}  SL={short_sl}  TP={short_tp}")
                sell_count += 1
                summary_lines.append(f"  {symbol}: {'[PAPER]' if PAPER_TRADE else ''}指値ショート @ {short_limit:.5f}")

            # ── ロング決済 ──────────────────────────────────────────────
            elif decision["action"] == "sell" and pos_side == "BUY":
                if PAPER_TRADE:
                    close_position_paper(symbol, price, decision["reason"])
                else:
                    gmo.close_position(position["positionId"], symbol, "SELL", int(position["size"]))
                    send_telegram(
                        f"✅ ロング決済\n通貨ペア: {symbol}\n価格: {price:.5f}\n理由: {decision['reason']}"
                    )
                logging.info(f"ロング決済: {symbol} @ {price:.5f}")
                sell_count += 1
                summary_lines.append(f"  {symbol}: ロング決済 @ {price:.5f}")

            # ── ショート決済 ────────────────────────────────────────────
            elif decision["action"] == "buy" and pos_side == "SELL":
                if PAPER_TRADE:
                    close_position_paper(symbol, price, decision["reason"])
                else:
                    gmo.close_position(position["positionId"], symbol, "BUY", int(position["size"]))
                    send_telegram(
                        f"✅ ショート決済\n通貨ペア: {symbol}\n価格: {price:.5f}\n理由: {decision['reason']}"
                    )
                logging.info(f"ショート決済: {symbol} @ {price:.5f}")
                buy_count += 1
                summary_lines.append(f"  {symbol}: ショート決済 @ {price:.5f}")

            else:
                logging.info(f"{symbol} 待機中")
                summary_lines.append(f"  {symbol}: 待機")

        except Exception as e:
            error_msg = f"⚠️ エラー発生\n通貨ペア: {symbol}\n内容: {str(e)}"
            logging.error(error_msg)
            send_telegram(error_msg)
            # 短時間に連続エラーが発生するのを防ぐため簡易バックオフ
            try:
                time.sleep(60)
            except Exception:
                pass

    # サマリー通知
    if not PAPER_TRADE:
        cash = gmo.get_cash_jpy()
        cash_str = f"¥{cash:,.0f}"
    else:
        cash_str = "（ペーパー）"

    summary = (
        f"📊 {'[PAPER] ' if PAPER_TRADE else ''}本日のサマリー\n"
        f"有効証拠金: {cash_str}\n"
        f"買い注文: {buy_count}件\n"
        f"決済注文: {sell_count}件\n"
        f"取引詳細:\n" + "\n".join(summary_lines)
    )
    send_telegram(summary)
    logging.info("=== ボット終了 ===")


if __name__ == "__main__":
    configure_logging(LOG_FILE)
    run_bot()
