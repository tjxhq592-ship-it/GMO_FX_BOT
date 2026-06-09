import json
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

# Telegram通知
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ダッシュボード認証
DASHBOARD_AUTH_ENABLED = os.getenv("DASHBOARD_AUTH_ENABLED", "false").lower() == "true"

# ファイルパス
PARAMS_FILE = BASE_DIR / "params.json"
LOG_FILE    = BASE_DIR / "trade_log.txt"

# 取引設定
TRADE_AMOUNT = 10000  # 円建て（GMO FXは円単位）

# 取引通貨ペアと候補リストを backtest_config.json から読み込む
def _load_bt_config() -> dict:
    _path = BASE_DIR / "backtest_config.json"
    if _path.exists():
        with open(_path, "r", encoding="utf-8") as _f:
            return json.load(_f)
    return {}

_bt_cfg = _load_bt_config()

# 取引通貨ペア: active_symbols（グリッドサーチ採用済みでトレード対象）
SYMBOLS = _bt_cfg.get("active_symbols", _bt_cfg.get("symbols", ["AUD_NZD"]))

# 選択可能な全ペア一覧
CANDIDATE_SYMBOLS = _bt_cfg.get("available_symbols", [
    "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY",
    "NZD_JPY", "CAD_JPY", "CHF_JPY", "ZAR_JPY",
    "EUR_USD", "GBP_USD", "AUD_USD", "EUR_GBP",
    "AUD_NZD", "EUR_CHF", "GBP_CHF", "EUR_AUD",
])

# バックテスト・グリッドサーチに必須
_REQUIRED_BASIC = {
    "GMO_API_KEY":    GMO_API_KEY,
    "GMO_SECRET_KEY": GMO_SECRET_KEY,
}

# トレードボット稼働時に必須
_REQUIRED_TRADE = {
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHAT_ID":   TELEGRAM_CHAT_ID,
}
def check_required_keys(raise_on_missing: bool = False) -> list:
    """
    Check required environment keys and return a list of missing keys.

    Parameters
    - raise_on_missing: if True and basic keys are missing, raise EnvironmentError.

    Returns
    - List[str]: list of missing environment variable names (may include trade keys).
    """
    missing_basic = [k for k, v in _REQUIRED_BASIC.items() if not v]
    missing_trade = [k for k, v in _REQUIRED_TRADE.items() if not v]

    if raise_on_missing and missing_basic:
        raise EnvironmentError(
            f"以下の環境変数が未設定です: {', '.join(missing_basic)}"
        )

    if missing_trade:
        import warnings
        warnings.warn(
            f"トレードボット用キーが未設定です: {', '.join(missing_trade)}"
        )

    return missing_basic + missing_trade
