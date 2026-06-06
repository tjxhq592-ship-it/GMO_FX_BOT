"""
Polymarket シグナル取得モジュール
Gamma API（認証不要）を使用してマーケット確率を取得し、
通貨ペアのリスク判定に使用する。
"""
import json
import os
import time

import requests

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CACHE_FILE = "polymarket_cache.json"
CACHE_TTL  = 6 * 3600   # 6時間（秒）
SURGE_THRESHOLD = 0.10   # 急変判定：10%以上の変動
RISK_THRESHOLD  = 0.80   # リスクブロック：80%超

# 通貨ペアとPolymarketキーワードの対応
SYMBOL_KEYWORDS: dict[str, list[str]] = {
    "EUR_GBP": ["ECB rate", "UK election", "Europe geopolitical"],
    "EUR_CHF": ["ECB rate", "Switzerland SNB", "Europe geopolitical"],
    "AUD_NZD": [],   # 流動性薄いためスキップ
}


# ── キャッシュ読み書き ────────────────────────────────────────────────────

def _load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# ── Gamma API でマーケット検索 ────────────────────────────────────────────

def _search_markets(keyword: str, limit: int = 5) -> list[dict]:
    """キーワードでマーケットを検索し、結果リストを返す。失敗時は空リスト。"""
    try:
        resp = requests.get(
            GAMMA_API,
            params={"q": keyword, "active": "true", "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else []
    except Exception:
        return []


def _get_yes_prob(market: dict) -> float | None:
    """outcomePrices[0] を Yes 確率（0〜1 float）として返す。取得不可時は None。"""
    try:
        prices = market.get("outcomePrices") or market.get("outcome_prices")
        if not prices:
            return None
        raw = prices[0]
        prob = float(raw)
        # Polymarket は 0〜1 で返すが、念のため 0〜100 も正規化
        return prob / 100.0 if prob > 1.0 else prob
    except (TypeError, ValueError, IndexError):
        return None


# ── 急変検知 ─────────────────────────────────────────────────────────────

def _check_surge(cache: dict, question: str, prob: float) -> tuple[bool, dict]:
    """
    前回値と比較して急変（SURGE_THRESHOLD 以上の変動）を検知する。
    戻り値: (surge_flag, 更新済みエントリ)
    """
    now = time.time()
    entry = cache.get(question)

    surge = False
    if entry:
        elapsed = now - entry.get("ts", 0)
        if elapsed <= CACHE_TTL:
            prev = entry.get("prob", prob)
            if abs(prob - prev) >= SURGE_THRESHOLD:
                surge = True

    new_entry = {"prob": prob, "ts": now}
    return surge, new_entry


# ── メイン関数 ────────────────────────────────────────────────────────────

def get_polymarket_signal(symbol: str) -> dict:
    """
    symbol に対応する Polymarket シグナルを返す。

    戻り値の型:
    {
        "enabled": bool,
        "markets": [{"question": str, "prob": float, "surge": bool}],
        "risk_block": bool,
        "surge_detected": bool,
    }
    """
    keywords = SYMBOL_KEYWORDS.get(symbol, [])

    # AUD_NZD またはキーワード未設定の場合は無効
    if not keywords:
        return {"enabled": False, "risk_block": False, "surge_detected": False}

    cache = _load_cache()
    markets_out: list[dict] = []
    risk_block     = False
    surge_detected = False

    for keyword in keywords:
        results = _search_markets(keyword)
        if not results:
            continue

        # 最上位マーケットのみ使用
        market   = results[0]
        question = market.get("question", keyword)
        prob     = _get_yes_prob(market)
        if prob is None:
            continue

        surge, new_entry       = _check_surge(cache, question, prob)
        cache[question]        = new_entry

        markets_out.append({"question": question, "prob": prob, "surge": surge})

        if prob > RISK_THRESHOLD:
            risk_block = True
        if surge:
            surge_detected = True

    _save_cache(cache)

    return {
        "enabled":        True,
        "markets":        markets_out,
        "risk_block":     risk_block,
        "surge_detected": surge_detected,
    }


if __name__ == "__main__":
    for sym in ["EUR_GBP", "EUR_CHF", "AUD_NZD"]:
        result = get_polymarket_signal(sym)
        print(f"\n=== {sym} ===")
        print(json.dumps(result, indent=2, ensure_ascii=False))
