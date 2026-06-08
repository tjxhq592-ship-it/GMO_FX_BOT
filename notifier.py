"""
Telegram通知モジュール（全ファイル共通）
send_telegram() を import して使う。
"""
import logging

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_telegram(message: str) -> None:
    """Telegram Bot API でメッセージを送信する。"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML",
        }
        r = requests.post(url, json=data, timeout=10)
        if r.status_code != 200:
            logging.error(f"Telegram送信失敗: {r.status_code} {r.text}")
    except Exception as e:
        logging.error(f"Telegram送信エラー: {e}")
