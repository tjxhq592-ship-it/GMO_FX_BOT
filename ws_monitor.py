"""
GMO FX WebSocket リアルタイム価格監視
wss://forex-api.coin.z.com/ws/public/v1

各通貨ペアの ticker を購読し、ATR ベースの SL/TP ラインと
現在価格を比較して、ラインを超えたら即決済する。
ペーパートレードモード時は paper_positions.json を監視する。
"""
import json
import logging
import os
import ssl
import time

import websocket

from config import (
    GMO_API_KEY, GMO_SECRET_KEY,
    LOG_FILE, PARAMS_FILE, SYMBOLS,
)
from gmo_client import GmoFxClient
from notifier import send_telegram
from trade_bot import (
    PAPER_TRADE,
    load_paper_position,
    close_position_paper,
)

WS_URL        = "wss://forex-api.coin.z.com/ws/public/v1"
RECONNECT_SEC = 5

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%y/%m/%d %H:%M:%S",
    encoding="utf-8",
)

gmo = GmoFxClient(GMO_API_KEY, GMO_SECRET_KEY, notify_fn=send_telegram)


def _load_params() -> dict:
    try:
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("params", {})
    except Exception:
        return {}


def _check_sl_tp(symbol: str, current_price: float) -> None:
    """現在価格が SL/TP ラインを超えていれば即決済。
    ペーパーモード: paper_positions.json を参照。
    本番モード: GMO API のポジション情報を参照。
    """
    params = _load_params()
    p = params.get(symbol)
    if not p:
        return

    if PAPER_TRADE:
        # ── ペーパーモード ──────────────────────────────────────────
        pos = load_paper_position(symbol)
        if not pos:
            return

        sl = pos.get("sl")
        tp = pos.get("tp")
        if sl is None or tp is None:
            return

        if pos["side"] == "BUY":
            hit_sl = current_price <= sl
            hit_tp = current_price >= tp
        else:
            hit_sl = current_price >= sl
            hit_tp = current_price <= tp

        if hit_sl or hit_tp:
            trigger = "TP到達" if hit_tp else "SL到達"
            close_position_paper(symbol, current_price, trigger)
            logging.info(f"[PAPER] {trigger}: {symbol} @ {current_price:.5f}")

    else:
        # ── 本番モード ──────────────────────────────────────────────
        positions = gmo.get_open_positions(symbol)
        if not positions:
            return

        pos         = positions[0]
        entry_price = float(pos.get("price", current_price))
        atr_sl_mult = p.get("atr_sl_mult", 1.5)
        atr_tp_mult = p.get("atr_tp_mult", 2.5)

        try:
            from utils import calculate_atr
            import pandas as pd
            bars       = gmo.get_klines_range(symbol, interval="1hour", days=3)
            atr_series = calculate_atr(bars["high"], bars["low"], bars["close"],
                                       period=p.get("atr_period", 14))
            atr = float(atr_series.iloc[-1])
        except Exception:
            return

        if pos["side"] == "BUY":
            sl = entry_price - atr * atr_sl_mult
            tp = entry_price + atr * atr_tp_mult
            hit_sl = current_price <= sl
            hit_tp = current_price >= tp
        else:
            sl = entry_price + atr * atr_sl_mult
            tp = entry_price - atr * atr_tp_mult
            hit_sl = current_price >= sl
            hit_tp = current_price <= tp

        if hit_sl or hit_tp:
            trigger    = "TP到達" if hit_tp else "SL到達"
            close_side = "SELL" if pos["side"] == "BUY" else "BUY"
            try:
                gmo.close_position(pos["positionId"], symbol, close_side, int(pos["size"]))
                msg = (
                    f"🔔 [{trigger}] WebSocket即決済\n"
                    f"通貨ペア: {symbol}\n"
                    f"現在値: {current_price:.5f}\n"
                    f"建値: {entry_price:.5f}\n"
                    f"SL: {sl:.5f} / TP: {tp:.5f}"
                )
                logging.info(msg)
                send_telegram(msg)
            except Exception as e:
                logging.error(f"ws_monitor 決済エラー ({symbol}): {e}")


# ── WebSocket ハンドラ ────────────────────────────────────────────────────

def _on_open(ws: websocket.WebSocketApp) -> None:
    logging.info("WebSocket 接続完了")
    for symbol in SYMBOLS:
        subscribe_msg = json.dumps({
            "command": "subscribe",
            "channel": "ticker",
            "symbol":  symbol,
        })
        ws.send(subscribe_msg)
        logging.info(f"  購読開始: {symbol}")


def _on_message(ws: websocket.WebSocketApp, message: str) -> None:
    try:
        data = json.loads(message)
        symbol = data.get("symbol")
        if not symbol or symbol not in SYMBOLS:
            return

        ask = data.get("ask")
        bid = data.get("bid")
        if ask is None or bid is None:
            return
        current_price = (float(ask) + float(bid)) / 2.0

        if symbol == "CHF_JPY":
            logging.info(f"[DEBUG] CHF_JPY tick ask={ask} bid={bid} mid={current_price:.5f}")

        _check_sl_tp(symbol, current_price)

    except Exception as e:
        logging.error(f"ws_monitor メッセージ処理エラー: {e}")


def _on_error(ws: websocket.WebSocketApp, error: Exception) -> None:
    logging.error(f"WebSocket エラー: {error}")


def _on_close(ws: websocket.WebSocketApp, close_status_code, close_msg) -> None:
    logging.info(f"WebSocket 切断: {close_status_code} {close_msg}")


# ── エントリーポイント ────────────────────────────────────────────────────

def start_ws_monitor() -> None:
    """WebSocket 監視を開始（切断時は自動再接続）"""
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            ws.run_forever(
                ping_interval=30,
                ping_timeout=10,
                sslopt={"cert_reqs": ssl.CERT_NONE},
            )
        except Exception as e:
            logging.error(f"WebSocket 予期せぬエラー: {e}")

        logging.info(f"WebSocket {RECONNECT_SEC}秒後に再接続...")
        time.sleep(RECONNECT_SEC)


if __name__ == "__main__":
    from logger_config import configure_logging
    configure_logging(LOG_FILE)
    start_ws_monitor()
