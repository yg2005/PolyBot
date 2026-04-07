# Phase 2 — Data Feeds

Read CONTEXT.md first. Phase 1 must be complete.

## Build

### 1. Base feed (src/kalbot/feeds/base.py)
Abstract class: connect(), disconnect(), on_update(). Health: last_update_time, is_stale property. Auto-reconnect with exponential backoff (1s→2s→4s, max 30s). FeedStatus enum: CONNECTING, CONNECTED, STALE, DISCONNECTED.

### 2. Chainlink feed (src/kalbot/feeds/chainlink.py)
- WebSocket to wss://ws-live-data.polymarket.com
- Subscribe: `{"type": "subscribe", "topic": "crypto_prices_chainlink", "filter": {"symbol": "btc/usd"}}`
- Messages: `{"type": "crypto_price_chainlink", "symbol": "btc/usd", "price": 66743.29}`
- Parse: `float(payload["price"])` — plain USD, no scaling
- Push-based ~1 update/sec. Ping every 5s. Stale if no update in 10s.
- Reconnect with 3s backoff on any error. Catch gaierror specifically for DNS failures.
- Emit PriceUpdate(price, timestamp, source="chainlink")

### 3. Spot feed (src/kalbot/feeds/spot_feed.py)
- REST polling, NOT WebSocket (Binance WS geo-blocked HTTP 451)
- Primary: `GET https://api.coinbase.com/v2/prices/BTC-USD/spot`
- Fallback: `GET https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD`
- Poll every 2s. Coinbase first, Kraken if fail. Backoff up to 30s if both fail.
- Maintains rolling deque(~120 samples) for trend_1m calculation
- trend_1m: price_now vs price_at(now - 60s) anchored to wall-clock, NOT sample count
- Emit PriceUpdate(price, timestamp, source="coinbase"/"kraken")

### 4. Polymarket client (src/kalbot/feeds/polymarket.py)

**Discovery (Gamma API):**
- `GET https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500&order=endDate&ascending=true&end_date_min={now}&end_date_max={now+15min}`
- Filter: title contains "Bitcoin" AND ("5-minute" or "5 min"), series_ticker=="POLYBTC5M" or ticker prefix "POLY-"
- Poll every 30s
- Market JSON has: id, question, conditionId, endDate, clobTokenIds[0]=YES [1]=NO, outcomePrices, makerBaseFee, restricted
- clobTokenIds are ERC-1155 token IDs used in all CLOB calls

**Orderbook (CLOB API):**
- `GET https://clob.polymarket.com/midpoint?token_id={yes_token_id}`
- `GET https://clob.polymarket.com/spread?token_id={yes_token_id}`
- `GET https://clob.polymarket.com/price?token_id={yes_token_id}&side=buy`
- `GET https://clob.polymarket.com/book?token_id={yes_token_id}` (full levels)

**Settlement:** Market becomes closed=true, disappears from active query. Bot must capture Chainlink price at endDate. YES wins if BTC > strike.

### 5. Feed coordinator (update main.py)
- Start all feeds concurrently
- `feeds_ready` flag: only True when Chainlink got ≥1 price, spot got ≥1 price, Polymarket discovered ≥1 market. No scoring until ready.
- `network_healthy` flag: if any feed gets gaierror, set False on ALL feeds. Resume only when ALL recover.
- If Chainlink OR Polymarket stale >10s → pause trading. If only spot stale → continue, spot_confirms=False.

## Done when
- Bot starts, connects to Chainlink WS, prints BTC prices every second
- Spot feed prints Coinbase/Kraken price every 2s
- Polymarket discovers active BTC5M market, prints market question + YES/NO prices
- Feeds recover automatically after simulated disconnect
