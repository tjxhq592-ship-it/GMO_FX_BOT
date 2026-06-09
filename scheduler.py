"""
GMO FX Bot スケジューラ
・WebSocket 監視スレッド（常時稼働）
・毎時エントリー判断（月〜金 06:00〜土 06:00 JST, 土日スキップ）
・毎週月曜 06:30 週次バックテスト
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import schedule

from config import LOG_FILE, SYMBOLS, GMO_API_KEY, GMO_SECRET_KEY
from gmo_client import GmoFxClient
from notifier import send_telegram
from trade_bot import PAPER_TRADE
import trade_bot
import ws_monitor
import backtest as bt_module

JST = timezone(timedelta(hours=9))

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
BT_CONFIG_FILE = os.path.join(BASE_DIR, "backtest_config.json")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%y/%m/%d %H:%M:%S",
    encoding="utf-8",
)



# ── 起動時ポジション同期 ─────────────────────────────────────────────────
def init_position_sync() -> None:
    if PAPER_TRADE:
        return  # ペーパーモードではAPIポジション確認不要

    gmo = GmoFxClient(GMO_API_KEY, GMO_SECRET_KEY, notify_fn=send_telegram)
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

    send_telegram("🔍 起動時ポジション確認\n" + "\n".join(lines))


# ── FX市場時間チェック ────────────────────────────────────────────────────
def _is_fx_market_open() -> bool:
    now     = datetime.now(JST)
    weekday = now.weekday()
    if weekday == 6:
        return False
    if weekday == 5 and now.hour >= 6:
        return False
    if weekday == 0 and now.hour < 6:
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
    schedule.every().hour.at(":00").do(_entry_job)
    schedule.every().hour.at(":30").do(_entry_job)
    schedule.every().monday.at("06:30").do(_backtest_job)


# ── メイン ───────────────────────────────────────────────────────────────
def _read_bt_config() -> dict:
    try:
        with open(BT_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main() -> None:
    _cfg      = _read_bt_config()
    _symbols  = _cfg.get("active_symbols", _cfg.get("symbols", SYMBOLS))
    now_jst   = datetime.now(JST)
    next_hour = now_jst.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    next_str  = next_hour.strftime("%H:%M")
    pairs     = " / ".join(_symbols)

    if PAPER_TRADE:
        startup_msg = (
            f"📝 ペーパートレードモードで起動\n"
            f"監視ペア: {pairs}\n"
            f"次回エントリー判断: {next_str}\n"
            f"※実際の発注は行いません"
        )
    else:
        print("=" * 50)
        print("⚠️  本番トレードモードで起動します")
        print("⚠️  実際の発注が行われます")
        print("=" * 50)
        startup_msg = (
            f"🚀 本番トレードモードで起動\n"
            f"監視ペア: {pairs}\n"
            f"次回エントリー判断: {next_str}"
        )

    logging.info(startup_msg)
    send_telegram(startup_msg)

    init_position_sync()

    ws_thread = _start_ws_thread()
    _setup_schedule()
    logging.info("スケジューラ 起動完了")

    while True:
        if not ws_thread.is_alive():
            logging.warning("WebSocketスレッドが停止。再起動します。")
            ws_thread = _start_ws_thread()
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    from logger_config import configure_logging
    configure_logging(LOG_FILE)
    main()
