"""
GMOコイン 外国為替FX API クライアント
Alpaca の TradingClient / StockHistoricalDataClient に相当するラッパー
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

import pandas as pd
import requests


BASE_PUBLIC  = "https://forex-api.coin.z.com/public"
BASE_PRIVATE = "https://forex-api.coin.z.com/private"


class GmoFxClient:
    def __init__(self, api_key: str, secret_key: str):
        self.api_key    = api_key
        self.secret_key = secret_key

    # ── 認証ヘッダー生成 ────────────────────────────────────────────────
    def _sign(self, method: str, path: str, body: dict | None = None) -> dict:
        ts   = str(int(time.time() * 1000))
        body_str = json.dumps(body) if body else ""
        text = ts + method + path + body_str
        sign = hmac.new(
            self.secret_key.encode(), text.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "API-KEY":       self.api_key,
            "API-TIMESTAMP": ts,
            "API-SIGN":      sign,
            "Content-Type":  "application/json",
        }

    # ── Public API ───────────────────────────────────────────────────────
    def get_ticker(self, symbol: str) -> dict:
        """最新レート取得"""
        r = requests.get(
            f"{BASE_PUBLIC}/v1/ticker",
            params={"symbol": symbol},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data["status"] != 0:
            raise RuntimeError(f"ticker error: {data}")
        return data["data"][0]

    def get_klines(
        self,
        symbol: str,
        interval: str = "1day",
        date: str | None = None,
    ) -> pd.DataFrame:
        """
        KLine（ローソク足）取得
        interval: 1min / 5min / 10min / 15min / 30min / 1hour /
                  4hour / 8hour / 12hour / 1day / 1week / 1month
        date: YYYYMMDD（日足以上は YYYY、週足は YYYYMMM なども可）
        """
        if date is None:
            date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        r = requests.get(
            f"{BASE_PUBLIC}/v1/klines",
            params={"symbol": symbol, "interval": interval, "date": date},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data["status"] != 0:
            raise RuntimeError(f"klines error: {data}")
        rows = data["data"]
        df = pd.DataFrame(rows, columns=["openTime", "open", "high", "low", "close", "volume"])
        df["openTime"] = pd.to_datetime(df["openTime"].astype(int), unit="ms", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df.set_index("openTime", inplace=True)
        return df

    def get_klines_range(
        self,
        symbol: str,
        interval: str = "1day",
        days: int = 90,
    ) -> pd.DataFrame:
        """複数日のKLineを結合して返す（日足の場合は各日付で1回ずつ取得）"""
        frames = []
        for i in range(days, 0, -1):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            try:
                df = self.get_klines(symbol, interval, d)
                frames.append(df)
            except Exception:
                pass
            time.sleep(0.2)  # レート制限対策
        if not frames:
            raise RuntimeError("KLine取得失敗")
        return pd.concat(frames).sort_index().drop_duplicates()

    # ── Private API ──────────────────────────────────────────────────────
    def get_assets(self) -> dict:
        """資産残高取得"""
        path = "/v1/account/assets"
        r = requests.get(
            BASE_PRIVATE + path,
            headers=self._sign("GET", path),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data["status"] != 0:
            raise RuntimeError(f"assets error: {data}")
        return data["data"]

    def get_open_positions(self, symbol: str | None = None) -> list:
        """建玉一覧取得"""
        path = "/v1/openPositions"
        params = {}
        if symbol:
            params["symbol"] = symbol
        r = requests.get(
            BASE_PRIVATE + path,
            headers=self._sign("GET", path),
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data["status"] != 0:
            raise RuntimeError(f"positions error: {data}")
        return data.get("data", {}).get("list", [])

    def place_order(
        self,
        symbol: str,
        side: str,          # "BUY" or "SELL"
        size: int,          # 取引数量（1000通貨単位）
        order_type: str = "MARKET",
        price: float | None = None,
    ) -> dict:
        """注文発注"""
        path = "/v1/order"
        body: dict = {
            "symbol":    symbol,
            "side":      side,
            "executionType": order_type,
            "size":      str(size),
        }
        if price is not None:
            body["price"] = str(price)
        r = requests.post(
            BASE_PRIVATE + path,
            headers=self._sign("POST", path, body),
            json=body,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data["status"] != 0:
            raise RuntimeError(f"order error: {data}")
        return data["data"]

    def close_position(self, position_id: str, symbol: str, side: str, size: int) -> dict:
        """決済注文"""
        path = "/v1/closeOrder"
        body = {
            "symbol":        symbol,
            "side":          side,          # 建玉と逆サイド
            "executionType": "MARKET",
            "settlePosition": [
                {"positionId": position_id, "size": str(size)}
            ],
        }
        r = requests.post(
            BASE_PRIVATE + path,
            headers=self._sign("POST", path, body),
            json=body,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data["status"] != 0:
            raise RuntimeError(f"close order error: {data}")
        return data["data"]

    def get_cash_jpy(self) -> float:
        """日本円の有効証拠金を返す"""
        assets = self.get_assets()
        for a in assets:
            if a.get("symbol") == "JPY":
                return float(a.get("availableAmount", 0))
        return 0.0
