# Binance USDT-M Futures Volume Scanner Bot

A real-time scanner for Binance perpetual futures that detects high-probability breakout setups using a multi-layer filter strategy and sends alerts to a Telegram channel.

---

## How It Works

The bot scans all ~544 USDT-M perpetual futures pairs every 15 minutes on the 1-hour timeframe. Each coin must pass **all 13 filters** in sequence to generate an alert. If any filter fails, the coin is rejected and logged with the reason.

---

## Filter Criteria (in order)

### 1. Market Cap Filter
- **Setting:** `market_cap_max_usd: 1,000,000,000` ($1 billion)
- Only coins with a market cap **at or below $1B** are scanned
- Filters out large-caps (BTC, ETH, BNB, etc.) where volume spikes have less explosive potential
- Coins with unknown market cap are included by default (`include_unknown_market_cap: true`)

---

### 2. Volume Spike (Baseline Multiplier)
- **Setting:** `volume_multiplier: 3.0`, `volume_baseline_candles: 20`
- The current 1h candle's volume must be **at least 3x** the average volume of the previous 20 candles
- Detects abnormal volume surges that often precede significant price moves

---

### 3. RVOL Threshold (Relative Volume)
- **Setting:** `rvol_threshold: 4.0`
- The volume ratio must be **at least 4x** the baseline
- A stricter version of filter #2 — ensures the spike is genuinely significant, not a minor blip
- Both filter #2 and #3 must pass (4x satisfies both)

---

### 4. Minimum Absolute Volume
- **Setting:** `min_volume_usdt: 1,000,000` ($1 million)
- The recent candle's volume must be **at least $1M USDT**
- Eliminates low-liquidity coins where spikes are meaningless and slippage is high

---

### 5. Consecutive Volume Growth
- **Setting:** `consecutive_volume_candles: 3`
- The last 3 consecutive 1h candles must each have **higher volume than the previous**
- Confirms sustained accumulation rather than a single isolated spike
- Pattern: candle[n-2] < candle[n-1] < candle[n]

---

### 6. 24h Price Change Cap
- **Setting:** `max_price_change_24h_pct: 20.0`
- The coin's 24-hour price change must be **no more than ±20%**
- Avoids late entries on coins that have already made their big move
- Catches setups early, before the main pump

---

### 7. Bullish (Green) Candle
- **Setting:** `bullish_candle_required: true`
- The current candle must close **above its open** (green candle)
- No signals on red candles — momentum must be upward at entry

---

### 8. Upper Wick Filter
- **Setting:** `max_upper_wick_pct: 40`
- The upper wick must be **no more than 40%** of the total candle range
- Upper wick = distance from close to high
- A large upper wick indicates selling/rejection at the top — bad entry signal
- Example: if candle range is 100 pips, upper wick must be ≤ 40 pips

---

### 9. Candle Body Strength
- **Setting:** `min_body_pct: 50`
- The candle body (open to close) must be **at least 50%** of the total candle range
- Ensures the candle is a strong, decisive move — not a doji or spinning top
- Weak-bodied candles show indecision and are unreliable for breakout entries

---

### 10. Trend Strength
- **Settings:** `min_trend_green_pct: 60`, `trend_candles: 5`
- At least **60% of the last 5 candles** must be green (3 out of 5)
- Confirms the coin is in a short-term uptrend, not just a one-candle spike in a downtrend

---

### 11. Breakout Confirmation
- **Settings:** `breakout_enabled: true`, `breakout_lookback: 15`, `min_breakout_margin_pct: 0.5`, `max_breakout_margin_pct: 8.0`
- The current close must be **above the highest high** of the previous 15 candles
- Breakout margin must be between **0.5% and 8%** above that level:
  - Below 0.5%: too small — barely broke out, likely a false breakout
  - Above 8%: too large — buying the top, missed the entry
- This is the most powerful filter — confirms a genuine resistance break with volume

---

### 12. Open Interest Surge
- **Settings:** `open_interest_enabled: true`, `open_interest_periods: 15`, `open_interest_min_increase_pct: 5.0`
- Open interest must have **increased by at least 5%** over the last 15 periods
- Rising OI alongside rising price = new money entering the market (strong signal)
- Falling OI = short covering only (weaker signal) — rejected
- If OI data is unavailable for a coin, it is skipped

---

### 13. Weekly Liquidity Expansion
- **Setting:** `weekly_volume_multiplier: 2.5`
- The coin's 24h volume must be **at least 2.5x** its average daily volume over the past 7 days
- Confirms the volume spike is significant on a weekly scale, not just a local 1h event
- Prevents false signals from coins with naturally erratic hourly volume patterns

---

## Alert Contents

When all 13 filters pass, a Telegram alert is sent containing:

- Coin name and timeframe
- Volume spike ratio (e.g. 7.8x)
- Candle details: color, body %, wick %
- Breakout level and margin %
- Open interest change %
- Weekly RVOL
- 24h price change
- Market cap
- Entry price
- BTC price at time of alert
- Candle timestamp

---

## Post-Alert Tracking

Once a signal fires, the **tracker** monitors the coin automatically:

- **Take profit alerts** at +5%, +10%, +15%, +20% from entry
- **Reversal alert** if price drops 5% from its peak after reaching +3% (protects profits)
- Tracks open signals for up to 7 days (`max_age_hours: 168`)
- Price updates every 5 minutes

---

## Cooldown

- **Setting:** `cooldown_hours: 12`
- After a coin triggers an alert, it cannot alert again for **12 hours**
- Prevents spam from the same coin repeatedly firing

---

## Excluded Symbols

- `USDCUSDT` and `BTCDOMUSDT` are permanently excluded
- USDCUSDT is a stablecoin pair (no breakout potential)
- BTCDOMUSDT is a BTC dominance index, not a tradeable coin

---

## Configuration File

All settings are in `config.json`. Key values:

| Setting | Value | Purpose |
|---|---|---|
| market_cap_max_usd | 1,000,000,000 | Max market cap |
| volume_multiplier | 3.0x | Min volume spike |
| rvol_threshold | 4.0x | Min RVOL |
| min_volume_usdt | $1,000,000 | Min absolute volume |
| consecutive_volume_candles | 3 | Consecutive growth |
| max_price_change_24h_pct | 20% | Max 24h move |
| max_upper_wick_pct | 40% | Max upper wick |
| min_body_pct | 50% | Min candle body |
| min_trend_green_pct | 60% | Min trend green % |
| breakout_lookback | 15 candles | Resistance lookback |
| min_breakout_margin_pct | 0.5% | Min breakout margin |
| max_breakout_margin_pct | 8.0% | Max breakout margin |
| open_interest_min_increase_pct | 5% | Min OI increase |
| weekly_volume_multiplier | 2.5x | Weekly RVOL |
| cooldown_hours | 12h | Alert cooldown |
| scan_interval_seconds | 900 | Scan every 15 min |
| timeframe | 1h | Candle timeframe |
