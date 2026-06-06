# GMO FX Bot — システム構造・ロジック仕様書

**作成日**: 2026-06-06
**対象リポジトリ**: `GMO_FX_BOT`
**用途**: 外部AIモデルへの完成度・堅牢性レビュー依頼用

---

## 1. システム概要とファイル構成

### 1-1. 目的

GMOコイン FX API を使用して、EUR/GBP・AUD/NZD・EUR/CHF の 3 通貨ペアを対象に
**ボリンジャーバンド + RSI の逆張り戦略** で自動売買を行う Python ボット。
週次バックテスト → パラメータ最適化 → リアルタイム売買 のサイクルを自動で回す構成。

### 1-2. ファイル構成と役割

```
GMO_FX_BOT/
├── scheduler.py          # 全体の起動エントリーポイント。スレッド管理・スケジューリング
├── trade_bot.py          # 毎時のエントリー判断・注文発注ロジック
├── ws_monitor.py         # WebSocket によるリアルタイム SL/TP 監視・即決済
├── backtest.py           # 週次バックテスト・パラメータ最適化・params.json 更新
├── polymarket.py         # Polymarket（予測市場）からマクロリスク情報を取得
├── gmo_client.py         # GMOコイン FX REST API ラッパー
├── utils.py              # テクニカル指標計算（RSI・ATR・BB・MACD等）
├── config.py             # 環境変数・定数の一元管理
├── dashboard.py          # Streamlit 製モニタリング画面（手動起動）
├── params.json           # 最適化済みパラメータ（バックテストが毎週更新）
├── backtest_results.json # バックテスト結果（エクイティカーブ等）
├── trade_log.txt         # Python logging によるトレードログ
├── polymarket_cache.json # Polymarket 確率の前回値キャッシュ（急変検知用）
├── .cache/               # yfinance ダウンロードデータのピクルスキャッシュ（TTL=1h）
├── .env                  # API キー等の秘匿情報（Git 管理外）
└── requirements.txt      # 依存パッケージ一覧
```

### 1-3. 主要依存パッケージ

| パッケージ | 用途 |
|---|---|
| `anthropic` | Claude API（売買判断の最終意思決定） |
| `backtesting` | バックテスト・グリッドサーチ最適化 |
| `yfinance` | バックテスト用 OHLCV 取得（1h足、直近720日） |
| `schedule` | 毎時ジョブのスケジューリング |
| `websocket-client` | リアルタイム価格受信 |
| `streamlit` | ダッシュボード |
| `requests` | GMO REST API・LINE 通知 |

---

## 2. データフローと処理シーケンス

### 2-1. システム起動フロー（`python scheduler.py`）

```
scheduler.py 起動
├── LINE通知: 「🚀 GMO FXボット起動 / 次回エントリー判断: HH:00」
├── [スレッド①] ws_monitor.start_ws_monitor()   ← daemon スレッド（常時稼働）
│   └── WebSocket 接続 → ticker 購読（3ペア）
│       → 価格受信ごとに SL/TP ライン判定 → 超えたら即決済
└── [メインスレッド] schedule ループ
    ├── 毎時 :00  → trade_bot.run_bot()           ← FX市場時間のみ
    └── 毎週月曜 06:30 → backtest.run_backtest_job()
```

**FX市場時間の定義（JST）**

- 月曜 06:00 〜 土曜 06:00 のみ実行
- 土曜 06:00 以降・日曜は全日スキップ

### 2-2. 毎時エントリー判断フロー（`trade_bot.run_bot()`）

```
各通貨ペアに対して順次実行:

1. get_market_data()
   └── GMO API: get_klines_range(interval="1day", days=90)
       → BB(upper/mid/lower) / RSI / ATR / ATR20期間平均 を計算

2. get_polymarket_signal()
   └── Gamma API 検索 → outcomePrices[0] を Yes確率として取得
       → polymarket_cache.json と比較して急変（6h以内10%超変動）を検知

3. Polymarket 急変チェック（ポジション保有中）
   └── surge_detected=True かつポジションあり → 即決済 & LINE通知 & continue

4. ask_claude()  ← Claude Sonnet-4-6
   └── BB値・RSI・ATR・レンジ判定・Polymarketコンテキストを含むプロンプト送信
       → {"action": "buy"/"sell"/"hold", "reason": "..."} を JSON で受信

5. ATR ベース SL/TP 算出
   ├── long_sl  = price - ATR × atr_sl_mult
   ├── long_tp  = price + ATR × atr_tp_mult
   ├── short_sl = price + ATR × atr_sl_mult
   └── short_tp = price - ATR × atr_tp_mult

6. 注文判断・発注
   ├── buy  + ポジションなし + bull → [Polymarket ブロックチェック] → 新規ロング
   ├── sell + ポジションなし + bear → [Polymarket ブロックチェック] → 新規ショート
   ├── sell + ロング保有中          → ロング決済
   └── buy  + ショート保有中        → ショート決済

7. サマリー LINE通知
```

### 2-3. WebSocket 監視フロー（`ws_monitor.py`）

```
WebSocket 接続: wss://forex-api.coin.z.com/ws/public/v1
└── subscribe: {"command": "subscribe", "channel": "ticker", "symbol": 各ペア}
    → on_message 受信ごとに:
        ├── (ask + bid) / 2 = 現在価格
        ├── get_open_positions() でポジション確認
        ├── ATR を再計算（直近3日の1時間足）
        ├── entry_price ± ATR × mult で SL/TP ライン算出
        └── 超過判定 → close_position() → LINE通知
            切断時: 5秒後に自動再接続（無限ループ）
```

### 2-4. 週次バックテストフロー（`backtest.py`）

```
各銘柄に対して順次:
├── yfinance で 1h足 直近720日取得（.cache/ にピクルス保存、TTL=1h）
├── 学習期間（直近720日 - 3ヶ月）でグリッドサーチ最適化（864パターン）
├── ウォークフォワードテスト（直近3ヶ月で Out-of-Sample 検証）
├── フォワードテスト（直近90日で最適パラメータを適用）
├── エクイティカーブ生成
└── 除外判定（WFTシャープ<0 or PF<1.2 or 取引回数<200）
    → params.json / backtest_results.json を更新
```

---

## 3. エントリー・エグジットのロジック詳細

### 3-1. 使用インジケーターと計算方法（`utils.py`）

| インジケーター | 計算式 |
|---|---|
| **RSI** | `delta.clip(lower=0).rolling(period).mean() / (-delta.clip(upper=0)).rolling(period).mean()` によるWilder式近似 |
| **Bollinger Band** | `SMA(period) ± std_mult × rolling_std(period)` |
| **ATR** | `max(H-L, |H-前終値|, |L-前終値|)` の `rolling(period).mean()` |

### 3-2. バックテスト戦略: `ImprovedStrategy`（BB + RSI 逆張り）

#### エントリー条件（3条件すべてを満たすこと）

| 方向 | 条件① | 条件② | 条件③ |
|---|---|---|---|
| **ロング** | `Close < BB_lower` | `RSI ≤ rsi_lower` | `ATR[-1] < ATR[-20:].mean() × atr_range_mult` |
| **ショート** | `Close > BB_upper` | `RSI ≥ rsi_upper` | 同上（レンジ相場確認） |

#### エグジット条件

| ポジション | 条件 |
|---|---|
| ロング決済 | `Close > BB_mid`（中心線回帰）または SL/TP 到達 |
| ショート決済 | `Close < BB_mid` または SL/TP 到達 |
| 反対シグナル発生時 | 先に現ポジションをクローズ後、逆方向エントリー |

#### SL/TP の設定（ATRベース）

```
ロング : SL = entry - ATR × atr_sl_mult  /  TP = entry + ATR × atr_tp_mult
ショート: SL = entry + ATR × atr_sl_mult  /  TP = entry - ATR × atr_tp_mult
```

#### デフォルトパラメータ（params.json 初期値）

| パラメータ | 値 | 意味 |
|---|---|---|
| `bb_period` | 20 | BB計算期間 |
| `bb_std` | 2.0 | BB標準偏差倍率 |
| `rsi_period` | 14 | RSI計算期間 |
| `rsi_upper` | 70 | 買われすぎ閾値 |
| `rsi_lower` | 30 | 売られすぎ閾値 |
| `atr_period` | 14 | ATR計算期間 |
| `atr_sl_mult` | 1.5 | SL幅（ATR倍率） |
| `atr_tp_mult` | 2.0 | TP幅（ATR倍率） |
| `atr_range_mult` | 1.0 | レンジ判定閾値 |
| `trade_size` | 0.2 | 発注サイズ（資金の20%） |

#### 最適化グリッドサーチ（週次バックテスト時）

| パラメータ | 候補値 | 通り数 |
|---|---|---|
| `bb_period` | range(10, 30, 5) | 4 |
| `bb_std` | [1.5, 2.0, 2.5] | 3 |
| `rsi_period` | [14] | 1（固定） |
| `rsi_upper` | [65, 70, 75] | 3 |
| `rsi_lower` | [25, 30, 35] | 3 |
| `atr_period` | [14] | 1（固定） |
| `atr_sl_mult` | [1.5, 2.0] | 2 |
| `atr_tp_mult` | [2.0, 2.5] | 2 |
| `atr_range_mult` | [0.8, 1.0] | 2 |
| **合計** | | **864パターン** |

最適化指標: **シャープレシオ最大化**

#### パラメータ採用基準（除外条件）

以下のいずれかに該当する通貨ペアは `params.json` から除外:

- WFT シャープレシオ < 0
- Profit Factor < 1.2
- 取引回数 < 200（バックテスト期間全体）

### 3-3. ライブ取引時の追加フィルター（`trade_bot.py`）

| フィルター | 内容 |
|---|---|
| **市場環境** | `get_market_condition()` が `"bull"` の時のみロング、`"bear"` の時のみショート |
| **Polymarket リスクブロック** | `risk_block=True`（確率80%超）または `surge_detected=True`（急変）でエントリースキップ |
| **Claude 最終判断** | 上記データをすべて渡して Claude Sonnet-4-6 に buy/sell/hold を判断させる |

### 3-4. Polymarket シグナル（`polymarket.py`）

| 通貨ペア | 検索キーワード |
|---|---|
| EUR_GBP | "ECB rate" / "UK election" / "Europe geopolitical" |
| EUR_CHF | "ECB rate" / "Switzerland SNB" / "Europe geopolitical" |
| AUD_NZD | 無効（流動性薄いためスキップ） |

- `outcomePrices[0]` を Yes 確率（0〜1）として取得
- 前回値（`polymarket_cache.json`）と比較し、**6h 以内に 10% 以上変動**で急変フラグ `True`
- いずれかのマーケットが **80% 超**で `risk_block=True`

---

## 4. ポジション管理および二重発注防止メカニズム

### 4-1. ポジション追跡の仕組み

ローカルのフラグや変数でポジションを管理**しない**。毎回 GMO API の `/v1/openPositions` をリアルタイムに照会する設計。

```python
def get_position(symbol: str) -> dict | None:
    positions = gmo.get_open_positions(symbol)
    return positions[0] if positions else None
```

### 4-2. 二重発注防止の実装状況

| 状況 | 防止策 |
|---|---|
| ロング保有中に buy シグナル | `position is None` の条件ガードで弾く |
| ショート保有中に sell シグナル | 同上 |
| ロング保有中に sell シグナル | 決済処理に分岐（新規ショートは建てない） |
| ショート保有中に buy シグナル | 決済処理に分岐（新規ロングは建てない） |

> ⚠️ **未実装の防止機構**
> - スレッド間のロック機構（mutex/semaphore）がない。WebSocket スレッドと毎時エントリースレッドが同時に同一シンボルの決済を実行する**競合リスク**が存在する
> - `get_open_positions()` と `place_order()` の間の TOCTOU 問題に対する原子性保証がない
> - 注文 ID を保存・照合する仕組みがなく、部分約定や約定漏れの検出が不可能

### 4-3. パラメータ急変チェック（`check_param_change()`）

バックテスト更新時にパラメータが急変した場合、前回値を維持する保守機構。

```python
PARAM_LIMITS = {
    "rsi_upper":       0.15,   # ±15% 以内の変動のみ許容
    "rsi_lower":       0.15,
    "stop_loss_pct":   0.05,
    "take_profit_pct": 0.10,
    ...
}
```

> ⚠️ **陳腐化**: `PARAM_LIMITS` の定義が旧 EMA パラメータ名（`ma_short`、`stop_loss_pct` 等）のままであり、
> 現在の BB+RSI 戦略パラメータ（`bb_period`、`bb_std`、`atr_sl_mult` 等）は急変チェックの**対象外**になっている。

---

## 5. エラーハンドリング・例外処理・セキュリティの実装状況

### 5-1. エラーハンドリング一覧

#### `trade_bot.py` — 銘柄単位の try-except

1銘柄でエラーが発生しても他の銘柄の処理を継続する。エラー内容は `logging.error()` と LINE 通知の両方に送出。

```python
for symbol in SYMBOLS:
    try:
        ...
    except Exception as e:
        logging.error(error_msg)
        send_line(error_msg)
```

#### `gmo_client.py` — HTTP レベルのエラー処理

```python
r.raise_for_status()           # 4xx/5xx を HTTPError として送出
if data["status"] != 0:
    raise RuntimeError(...)    # GMO API 業務エラーを RuntimeError に変換
```

全 `requests` 呼び出しに `timeout=10` を設定。**リトライ機構は未実装**。

#### `gmo_client.get_klines_range()` — 日次データ取得の個別エラー吸収

```python
for i in range(days, 0, -1):
    try:
        frames.append(self.get_klines(...))
    except Exception:
        pass          # 1日分の取得失敗は無視して継続
    time.sleep(0.2)   # レートリミット対策
```

#### `ws_monitor.py` — 切断時の自動再接続

```python
while True:
    try:
        ws.run_forever(ping_interval=30, ping_timeout=10)
    except Exception as e:
        logging.error(...)
    time.sleep(5)     # 5秒後に再接続
```

#### `polymarket.py` — 外部 API 失敗の無害化

取得失敗時は空リストを返してシグナルなしで処理を継続（ボット全体には影響しない）。

#### `backtest.py` — 全銘柄エラー時の早期終了

```python
if not raw_results:
    print("全銘柄でエラーが発生しました。")
    raise SystemExit(1)
```

### 5-2. ログ管理

| 対象 | 方式 |
|---|---|
| ライブ取引 | Python `logging` → `trade_log.txt`（INFO レベル以上、ローテーションなし） |
| バックテスト | `print()` で標準出力のみ（ファイル記録なし） |
| 主要イベント | LINE Messaging API でリアルタイム通知（起動・発注・決済・エラー） |

### 5-3. セキュリティ

#### API キー管理

```python
# config.py
load_dotenv(BASE_DIR / ".env")
GMO_API_KEY    = os.getenv("GMO_API_KEY")
GMO_SECRET_KEY = os.getenv("GMO_SECRET_KEY")

# 未設定キーの起動時チェック
_missing = [k for k, v in _REQUIRED_KEYS.items() if not v]
if _missing:
    raise EnvironmentError(...)   # 不完全な設定での起動を拒否
```

- `.env` は `.gitignore` に含まれており Git 管理外
- GMO API の署名は `HMAC-SHA256`（ミリ秒タイムスタンプ付き）で実装済み

> ⚠️ `params.json` と `trade_log.txt` は **Git 管理対象**になっており、
> 取引状態・パラメータが外部リポジトリに公開される状態。

### 5-4. 既知の未実装事項（レビュー推奨ポイント）

| 項目 | 現状・リスク |
|---|---|
| **スレッド間の競合制御** | mutex/lock なし。`ws_monitor` と `trade_bot` が同タイミングで同一シンボルを決済する競合リスクあり |
| **リトライ機構** | API タイムアウト・5xx エラーに対するリトライなし |
| **注文 ID 管理** | 発注後の約定確認・部分約定検出なし |
| **`PARAM_LIMITS` の陳腐化** | 旧 EMA パラメータ名のまま。BB+RSI パラメータは急変チェック対象外 |
| **`run_backtest_job()` の実装** | `subprocess.run([sys.executable, __file__])` で自身を再起動する形式。環境変数・作業ディレクトリ依存のリスクあり |
| **WebSocket の SL/TP ATR 取得** | ポジション建値時の ATR ではなく直近3日の再計算値を使用。建値時 ATR とずれる可能性あり |
| **ログのローテーション** | `trade_log.txt` は無制限に肥大化する |
| **`params.json` の Git 公開** | 取引パラメータが外部に公開される |
