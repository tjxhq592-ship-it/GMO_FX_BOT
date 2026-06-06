"""
GMO FX Bot スケジューラ
・WebSocket 監視スレッド（常時稼働）
・毎時エントリー判断（月〜金 06:00〜土 06:00 JST, 土日スキップ）
・毎週月曜 06:30 週次バックテスト
"""
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

import schedule
import requests

from config import (
    LOG_FILE, SYMBOLS,
    LINE_CHANNEL_TOKEN, LINE_USER_ID,
)
from config import GMO_API_KEY, GMO_SECRET_KEY
from gmo_client import GmoFxClient
import trade_bot
import ws_monitor
import backtest as bt_module

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)


# ── LINE通知（scheduler 専用。trade_bot.send_line と同実装）─────────────
def _send_line(message: str) -> None:
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


# ── 起動時ポジション同期 ─────────────────────────────────────────────────
def init_position_sync() -> None:
    """
    起動・再起動時に全ペアのポジション状態を API から取得してログ出力。
    ローカル状態との乖離を防ぎ、現状を把握してから処理を開始する。
    """
    gmo = GmoFxClient(GMO_API_KEY, GMO_SECRET_KEY, notify_fn=_send_line)
    logging.info("=== 起動時ポジション確認 ===")
    lines = []
    for symbol in SYMBOLS:
        try:
            positions = gmo.get_open_positions(symbol)
            if positions:
                for pos in positions:
                    line = (
                        f"  {symbol}: {pos.get('side')} "
                        f"{pos.get('size')}ロット @ {pos.get('price')}"
                    )
                    logging.info(line)
                    lines.append(line)
            else:
                logging.info(f"  {symbol}: ポジションなし")
                lines.append(f"  {symbol}: ポジションなし")
        except Exception as e:
            logging.error(f"  {symbol} ポジション確認エラー: {e}")
            lines.append(f"  {symbol}: 確認エラー ({e})")

    _send_line("🔍 起動時ポジション確認\n" + "\n".join(lines))


# ── FX市場時間チェック ────────────────────────────────────────────────────
def _is_fx_market_open() -> bool:
    """
    FX 市場時間: 月曜 06:00 JST 〜 土曜 06:00 JST
    土曜 06:00 以降・日曜は全日クローズ
    """
    now = datetime.now(JST)
    weekday = now.weekday()  # 0=月 ... 6=日

    if weekday == 6:  # 日曜
        return False
    if weekday == 5 and now.hour >= 6:  # 土曜 06:00 以降
        return False
    if weekday == 0 and now.hour < 6:   # 月曜 06:00 前
        return False
    return True


# ── エントリー判断ジョブ ─────────────────────────────────────────────────
def _entry_job() -> None:
    if not _is_fx_market_open():
        logging.info("FX市場クローズ中のためエントリー判断をスキップ")
        return
    logging.info("=== 毎時エントリー判断 開始 ===")
    try:
        trade_bot.run_bot()
    except Exception as e:
        logging.error(f"エントリー判断エラー: {e}")


# ── 週次バックテストジョブ ────────────────────────────────────────────────
def _backtest_job() -> None:
    logging.info("=== 週次バックテスト 開始 ===")
    try:
        bt_module.run_backtest_job()
    except Exception as e:
        logging.error(f"バックテストエラー: {e}")


# ── WebSocket 監視スレッド ────────────────────────────────────────────────
def _start_ws_thread() -> threading.Thread:
    t = threading.Thread(target=ws_monitor.start_ws_monitor, daemon=True, name="ws-monitor")
    t.start()
    logging.info("WebSocket監視スレッド 起動")
    return t


# ── スケジューラセットアップ ─────────────────────────────────────────────
def _setup_schedule() -> None:
    # 毎時 00 分にエントリー判断
    schedule.every().hour.at(":00").do(_entry_job)
    # 毎週月曜 06:30 に週次バックテスト
    schedule.every().monday.at("06:30").do(_backtest_job)


# ── メイン ───────────────────────────────────────────────────────────────
def main() -> None:
    now_jst = datetime.now(JST)
    # 次の :00 分を計算して案内
    next_hour = now_jst.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    next_str  = next_hour.strftime("%H:%M")

    pairs = " / ".join(SYMBOLS)
    startup_msg = (
        f"🚀 GMO FXボット起動\n"
        f"監視ペア: {pairs}\n"
        f"次回エントリー判断: {next_str}"
    )
    logging.info(startup_msg)
    _send_line(startup_msg)

    # 起動時に全ペアのポジション状態を確認
    init_position_sync()

    # WebSocket 監視スレッドを常時起動
    ws_thread = _start_ws_thread()

    # スケジュール登録
    _setup_schedule()
    logging.info("スケジューラ 起動完了")

    # メインループ
    while True:
        # WebSocket スレッドが落ちていたら再起動
        if not ws_thread.is_alive():
            logging.warning("WebSocketスレッドが停止。再起動します。")
            ws_thread = _start_ws_thread()

        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
