import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent

load_dotenv(BASE_DIR / ".env")

# GMOコイン FX API
GMO_API_KEY    = os.getenv("GMO_API_KEY")
GMO_SECRET_KEY = os.getenv("GMO_SECRET_KEY")

# Claude AI
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# LINE通知
LINE_CHANNEL_TOKEN = os.getenv("LINE_CHANNEL_TOKEN")
LINE_USER_ID       = os.getenv("LINE_USER_ID")

# ファイルパス
PARAMS_FILE = BASE_DIR / "params.json"
LOG_FILE    = BASE_DIR / "trade_log.txt"

# 取引設定
TRADE_AMOUNT = 10000  # 円建て（GMO FXは円単位）

# 取引通貨ペア（GMOコインFX対応ペア）
SYMBOLS = ["USD_JPY", "EUR_JPY", "GBP_JPY"]

CANDIDATE_SYMBOLS = [
    "USD_JPY", "EUR_JPY", "GBP_JPY",
    "AUD_JPY", "NZD_JPY", "CAD_JPY",
    "CHF_JPY", "ZAR_JPY", "EUR_USD",
    "GBP_USD", "AUD_USD",
]

_REQUIRED_KEYS = {
    "GMO_API_KEY":        GMO_API_KEY,
    "GMO_SECRET_KEY":     GMO_SECRET_KEY,
    "ANTHROPIC_API_KEY":  ANTHROPIC_API_KEY,
    "LINE_CHANNEL_TOKEN": LINE_CHANNEL_TOKEN,
    "LINE_USER_ID":       LINE_USER_ID,
}

_missing = [k for k, v in _REQUIRED_KEYS.items() if not v]
if _missing:
    raise EnvironmentError(
        f"以下の環境変数が .env に設定されていません: {', '.join(_missing)}\n"
        f".env.example を参考に .env ファイルを作成してください。"
    )
