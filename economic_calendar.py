"""
経済指標カレンダーモジュール
ForexFactory 公開JSON から週次カレンダーを取得し、
高インパクト指標前後のエントリースキップ判定に使用する。
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "economic_calendar_cache.json")
CACHE_TTL    = 3600  # 1時間（秒）

DEFAULT_SKIP_BEFORE = 1.0   # 発表N時間前からスキップ
DEFAULT_SKIP_AFTER  = 1.0   # 発表N時間後までスキップ

JST = timezone(timedelta(hours=9))

# 通貨ペア → 関連通貨リスト
SYMBOL_TO_CURRENCIES: dict[str, list[str]] = {
    "USD_JPY": ["USD", "JPY"],
    "EUR_JPY": ["EUR", "JPY"],
    "GBP_JPY": ["GBP", "JPY"],
    "AUD_JPY": ["AUD", "JPY"],
    "NZD_JPY": ["NZD", "JPY"],
    "CAD_JPY": ["CAD", "JPY"],
    "CHF_JPY": ["CHF", "JPY"],
    "TRY_JPY": ["TRY", "JPY"],
    "ZAR_JPY": ["ZAR", "JPY"],
    "MXN_JPY": ["MXN", "JPY"],
    "EUR_USD": ["EUR", "USD"],
    "GBP_USD": ["GBP", "USD"],
    "AUD_USD": ["AUD", "USD"],
    "NZD_USD": ["NZD", "USD"],
    "EUR_GBP": ["EUR", "GBP"],
    "EUR_CHF": ["EUR", "CHF"],
    "GBP_CHF": ["GBP", "CHF"],
    "EUR_AUD": ["EUR", "AUD"],
    "AUD_NZD": ["AUD", "NZD"],
}


# ── キャッシュ読み書き ────────────────────────────────────────────────────

def _load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ── ForexFactory JSON 取得 ────────────────────────────────────────────────

def _fetch_calendar() -> list[dict]:
    """ForexFactory JSONカレンダーを取得する。失敗時は空リスト。"""
    try:
        resp = requests.get(CALENDAR_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _get_events(impact_levels: list[str]) -> list[dict]:
    """
    キャッシュから有効なイベントリストを返す。
    キャッシュ切れの場合は再取得してキャッシュを更新する。
    """
    cache = _load_cache()
    now_ts = time.time()

    if cache.get("ts", 0) + CACHE_TTL > now_ts and cache.get("events"):
        events = cache["events"]
    else:
        events = _fetch_calendar()
        _save_cache({"ts": now_ts, "events": events})

    return [
        e for e in events
        if e.get("impact", "") in impact_levels
        and e.get("country", "").upper() != "ALL"
    ]


# ── イベント時刻パース ────────────────────────────────────────────────────

def _parse_event_utc(event: dict) -> datetime | None:
    """
    イベントの date フィールドを UTC datetime に変換する。
    ForexFactory は ISO 8601 形式（例: "2026-06-06T08:30:00-04:00"）で返す。
    """
    try:
        date_str = event.get("date", "")
        if not date_str:
            return None
        # Python 3.7+ の fromisoformat は UTC オフセット付きを解析できる
        dt = datetime.fromisoformat(date_str)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# ── メイン判定関数 ────────────────────────────────────────────────────────

def is_near_high_impact_event(
    symbol: str,
    skip_before: float = DEFAULT_SKIP_BEFORE,
    skip_after: float = DEFAULT_SKIP_AFTER,
    impact_levels: list[str] | None = None,
) -> tuple[bool, str]:
    """
    現在時刻がシンボル関連の高インパクト指標前後のウィンドウ内かを判定する。

    Returns:
        (should_skip: bool, reason: str)
    """
    if impact_levels is None:
        impact_levels = ["High"]

    currencies = SYMBOL_TO_CURRENCIES.get(symbol, [])
    if not currencies:
        return False, ""

    try:
        events = _get_events(impact_levels)
    except Exception:
        return False, ""

    now_utc = datetime.now(timezone.utc)

    for event in events:
        country = event.get("country", "").upper()
        if country not in currencies:
            continue

        event_dt = _parse_event_utc(event)
        if event_dt is None:
            continue

        diff_hours = (now_utc - event_dt).total_seconds() / 3600.0
        title = event.get("title", "不明")

        if -skip_before <= diff_hours <= skip_after:
            if diff_hours < 0:
                mins = int(-diff_hours * 60)
                reason = f"{country}: {title} 発表{mins}分前"
            else:
                mins = int(diff_hours * 60)
                reason = f"{country}: {title} 発表{mins}分後"
            return True, reason

    return False, ""


# ── Claude プロンプト用コンテキスト生成 ──────────────────────────────────

def build_calendar_context(symbol: str, lookahead_hours: float = 24) -> str:
    """
    Claude プロンプトに埋め込む経済指標コンテキストを生成する。
    今後 lookahead_hours 時間以内の高インパクト指標を列挙する。
    """
    currencies = SYMBOL_TO_CURRENCIES.get(symbol, [])
    if not currencies:
        return "（対象通貨の経済指標なし）"

    try:
        events = _get_events(["High", "Medium"])
    except Exception:
        return "（取得エラー）"

    now_utc = datetime.now(timezone.utc)
    lines: list[str] = []

    for event in events:
        country = event.get("country", "").upper()
        if country not in currencies:
            continue

        event_dt = _parse_event_utc(event)
        if event_dt is None:
            continue

        diff_hours = (event_dt - now_utc).total_seconds() / 3600.0
        if diff_hours < 0 or diff_hours > lookahead_hours:
            continue

        event_jst = event_dt.astimezone(JST)
        time_str = event_jst.strftime("%m/%d %H:%M JST")
        impact = event.get("impact", "")
        title = event.get("title", "不明")
        warning = " ⚠️1時間以内" if diff_hours < 1.0 else ""
        lines.append(f"  - {country}: {title} {time_str} ({impact}){warning}")

    if not lines:
        return "（今後24h以内に高インパクト指標なし）"
    return "\n".join(lines)


if __name__ == "__main__":
    print("=== 経済指標カレンダー テスト ===\n")
    test_symbols = ["USD_JPY", "EUR_JPY", "GBP_JPY", "EUR_USD", "AUD_USD"]
    for sym in test_symbols:
        skip, reason = is_near_high_impact_event(sym)
        cal = build_calendar_context(sym)
        print(f"--- {sym} ---")
        print(f"スキップ判定: {skip}  理由: {reason or 'なし'}")
        print(f"カレンダー:\n{cal}\n")
