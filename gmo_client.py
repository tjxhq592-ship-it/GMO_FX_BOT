"""
GMOコイン 外国為替FX API クライアント
- カスタム例外: GmoApiError / RateLimitError / MaintenanceError
- 指数バックオフリトライ（429: 5→10→20秒、503: 5分×3回）
- 注文系エラーは LINE 通知必須
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd
import requests


BASE_PUBLIC  = "https://forex-api.coin.z.com/public"
BASE_PRIVATE = "https://forex-api.coin.z.com/private"

MAX_RETRIES = 3


# ── カスタム例外 ──────────────────────────────────────────────────────────

class GmoApiError(Exception):
    """GMO API の全般的なエラー"""

class RateLimitError(GmoApiError):
    """429 レートリミット超過"""

class MaintenanceError(GmoApiError):
    """503 メンテナンス中"""


# ── クライアント ──────────────────────────────────────────────────────────

class GmoFxClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        notify_fn: Callable[[str], None] | None = None,
    ):
        self.api_key    = api_key
        self.secret_key = secret_key
        self._notify_fn = notify_fn   # LINE 通知などのコールバック
        # Use a requests.Session for connection pooling and performance
        self._session = requests.Session()

    # ── 認証ヘッダー生成 ────────────────────────────────────────────────
    def _sign(self, method: str, path: str, body: dict | None = None) -> dict:
        ts       = str(int(time.time() * 1000))
        body_str = json.dumps(body) if body else ""
        text     = ts + method + path + body_str
        sign     = hmac.new(
            self.secret_key.encode(), text.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "API-KEY":       self.api_key,
            "API-TIMESTAMP": ts,
            "API-SIGN":      sign,
            "Content-Type":  "application/json",
        }

    # ── リトライ付きリクエスト ───────────────────────────────────────────
    def _request(
        self,
        method: str,
        url: str,
        is_order: bool = False,
        **kwargs,
    ) -> dict:
        """
        指数バックオフ付きリトライ。
        429: 5→10→20秒待機後リトライ（最大3回）
        503: LINE通知 + 5分待機後リトライ（最大3回）
        その他エラー: GmoApiError を送出
        注文系（is_order=True）: エラー時 LINE 通知必須
        """
        for attempt in range(MAX_RETRIES):
            # HTTP リクエスト送信
            try:
                r = self._session.request(method, url, timeout=10, **kwargs)
            except requests.exceptions.Timeout:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise GmoApiError("リクエストタイムアウト（3回）")
            except requests.exceptions.RequestException as e:
                raise GmoApiError(str(e)) from e

            # 429: レートリミット
            if r.status_code == 429:
                # Respect Retry-After header when available
                ra = r.headers.get("Retry-After")
                try:
                    wait = int(ra) if ra is not None else 5 * (2 ** attempt)
                except Exception:
                    wait = 5 * (2 ** attempt)
                logging.warning(f"429 Rate limit. Retry-After={ra} wait={wait}s ({attempt+1}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    continue
                raise RateLimitError(f"レートリミット: {MAX_RETRIES}回リトライ後も失敗")

            # 503: メンテナンス
            if r.status_code == 503:
                logging.warning(f"503 Maintenance ({attempt+1}/{MAX_RETRIES}) Response: {r.text[:200]}")
                if self._notify_fn:
                    self._notify_fn("⚠️ GMO FX メンテナンス中\n5分後に再試行します")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(300)   # 5分待機
                    continue
                raise MaintenanceError(f"GMOメンテナンス: {MAX_RETRIES}回試行後も失敗")

            # その他 HTTP エラー
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                msg = f"HTTP {r.status_code}: {r.text[:300]}"
                logging.error(msg)
                if is_order and self._notify_fn:
                    self._notify_fn(f"⚠️ 注文HTTPエラー\n{msg}")
                raise GmoApiError(msg) from e

            # GMO 業務エラー
            data = r.json()
            if data.get("status") != 0:
                msg = f"GMO APIエラー status={data.get('status')}: {data}"
                logging.error(msg)
                if is_order and self._notify_fn:
                    self._notify_fn(f"⚠️ 注文APIエラー\n{msg}")
                raise GmoApiError(msg)

            return data

        raise GmoApiError("最大リトライ回数を超えました")

    # ── Public API ───────────────────────────────────────────────────────
    def get_ticker(self, symbol: str) -> dict:
        """最新レート取得"""
        data = self._request(
            "GET",
            f"{BASE_PUBLIC}/v1/ticker",
            params={"symbol": symbol},
        )
        for item in data["data"]:
            if item["symbol"] == symbol:
                return item
        raise GmoApiError(f"シンボルが見つかりません: {symbol}")

    def get_klines(
        self,
        symbol: str,
        interval: str = "1day",
        date: str | None = None,
    ) -> pd.DataFrame:
        """KLine（ローソク足）取得"""
        if date is None:
            date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        data = self._request(
            "GET",
            f"{BASE_PUBLIC}/v1/klines",
            params={"symbol": symbol, "priceType": "BID", "interval": interval, "date": date},
        )
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
        interval: str = "4hour",
        days: int = 90,
    ) -> pd.DataFrame:
        """後方互換ラッパー: days を years に変換して get_klines_bulk を呼び出す"""
        years = max(1, days // 365 + 1)
        return self.get_klines_bulk(symbol, interval=interval, years=years)

    # GMO API の date パラメータ形式によって取得単位が異なる
    _DAILY_INTERVALS  = {"1min", "5min", "10min", "15min", "30min", "1hour"}
    _YEARLY_INTERVALS = {"4hour", "8hour", "12hour", "1day", "1week", "1month"}

    def get_klines_bulk(
        self,
        symbol: str,
        interval: str = "30min",
        years: int = 2,
        price_type: str = "BID",
    ) -> pd.DataFrame:
        """複数年分の KLine データを取得して結合する。

        Parameters
        ----------
        symbol   : 通貨ペア（例: "USD_JPY"）
        interval : 足種（1min/5min/15min/30min/1hour/4hour/1day など）
        years    : 取得年数（直近 N 年分）

        Note
        ----
        _DAILY_INTERVALS  → 日単位（YYYYMMDD）でリクエスト
        _YEARLY_INTERVALS → 年単位（YYYY）でリクエスト
        2年分の日単位取得は約730リクエスト。キャッシュが効くため
        2回目以降は高速。
        """
        frames = []

        if interval in self._DAILY_INTERVALS:
            # 日単位で取得（YYYYMMDDフォーマット）
            # ※20231028以降のみ取得可能
            start   = datetime.now() - timedelta(days=365 * years)
            start   = max(start, datetime(2023, 10, 28))  # 取扱開始日
            current = start
            while current <= datetime.now():
                date_str = current.strftime("%Y%m%d")
                try:
                    df = self.get_klines(symbol, interval, date_str)
                    if not df.empty:
                        frames.append(df)
                except Exception:
                    pass
                current += timedelta(days=1)
                time.sleep(0.05)
        else:
            # 年単位で取得（YYYYフォーマット）
            current_year = datetime.now().year
            for year in range(current_year - years, current_year + 1):
                try:
                    df = self.get_klines(symbol, interval, str(year))
                    if not df.empty:
                        frames.append(df)
                    time.sleep(0.3)
                except Exception as e:
                    logging.warning(f"  {year}年データ取得失敗: {e}")

        if not frames:
            raise RuntimeError(f"{symbol} のデータ取得失敗")

        return pd.concat(frames).sort_index().drop_duplicates()

    # ── Private API ──────────────────────────────────────────────────────
    def get_assets(self) -> dict:
        """資産残高取得"""
        path = "/v1/account/assets"
        data = self._request(
            "GET",
            BASE_PRIVATE + path,
            headers=self._sign("GET", path),
        )
        return data["data"]

    def get_open_positions(self, symbol: str | None = None) -> list:
        """建玉一覧取得（常に API から最新状態を取得）"""
        path   = "/v1/openPositions"
        params = {"symbol": symbol} if symbol else {}
        data   = self._request(
            "GET",
            BASE_PRIVATE + path,
            headers=self._sign("GET", path),
            params=params,
        )
        return data.get("data", {}).get("list", [])

    def get_active_orders(self, symbol: str) -> list:
        """未約定の有効注文一覧を取得"""
        path   = "/v1/activeOrders"
        params = {"symbol": symbol, "executionType": "LIMIT"}
        data   = self._request(
            "GET",
            BASE_PRIVATE + path,
            headers=self._sign("GET", path),
            params=params,
        )
        return data.get("data", {}).get("list", [])

    def place_order(
        self,
        symbol: str,
        side: str,
        size: int,
        order_type: str = "MARKET",
        price: float | None = None,
    ) -> dict:
        """注文発注（成行 or 指値）"""
        path = "/v1/order"
        body: dict = {
            "symbol":        symbol,
            "side":          side,
            "executionType": order_type,
            "size":          str(size),
        }
        if price is not None:
            body["price"] = str(price)
        data = self._request(
            "POST",
            BASE_PRIVATE + path,
            is_order=True,
            headers=self._sign("POST", path, body),
            json=body,
        )
        return data["data"]

    def close_position(
        self,
        position_id: str,
        symbol: str,
        side: str,
        size: int,
    ) -> dict:
        """決済注文"""
        path = "/v1/closeOrder"
        body = {
            "symbol":        symbol,
            "side":          side,
            "executionType": "MARKET",
            "settlePosition": [
                {"positionId": position_id, "size": str(size)}
            ],
        }
        data = self._request(
            "POST",
            BASE_PRIVATE + path,
            is_order=True,
            headers=self._sign("POST", path, body),
            json=body,
        )
        return data["data"]

    def cancel_order(self, order_id: str) -> dict:
        """指値注文のキャンセル"""
        path = "/v1/cancelOrder"
        body = {"orderId": order_id}
        data = self._request(
            "POST",
            BASE_PRIVATE + path,
            is_order=True,
            headers=self._sign("POST", path, body),
            json=body,
        )
        return data.get("data", {})

    def get_cash_jpy(self) -> float:
        """日本円の有効証拠金を返す"""
        assets = self.get_assets()
        logging.debug("get_assets result type=%s", type(assets))

        # assetsがdictの場合
        if isinstance(assets, dict):
            return float(assets.get("availableAmount", 0))

        # assetsがlistの場合
        if isinstance(assets, list):
            for a in assets:
                if isinstance(a, dict) and a.get("symbol") == "JPY":
                    return float(a.get("availableAmount", 0))

        return 0.0
