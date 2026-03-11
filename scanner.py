"""
Core scanner engine.

Strategy (3 main conditions — ALL must pass to fire a signal):
  1. Current 1h candle closes above the highest high of the last 24 candles (24h breakout)
  2. Last 3 consecutive 1h candles have strictly increasing volume
  3. 24h price change is within ±20%

Additional data is collected at signal time for analysis but does NOT block the signal:
  - RVOL vs 20-candle baseline
  - Relative OI change vs 24h average
  - Funding rate
  - 24h volume (liquidity check)
  - Price vs 4h EMA50
  - Volatility compression score (last 10 vs prior 10 candles)
  - Breakout margin %
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from binance_client import BinanceClient
from market_cap import MarketCapProvider
from notifier import TelegramNotifier
from tracker import SignalTracker

logger = logging.getLogger(__name__)


class _CooldownTracker:
    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown = cooldown_seconds
        self._last_alert: Dict[str, float] = {}

    def is_on_cooldown(self, symbol: str) -> bool:
        last = self._last_alert.get(symbol)
        if last is None:
            return False
        remaining = self._cooldown - (time.time() - last)
        if remaining > 0:
            logger.debug("%s  on cooldown — %.1f min remaining", symbol, remaining / 60)
            return True
        return False

    def record(self, symbol: str) -> None:
        self._last_alert[symbol] = time.time()

    def prune(self) -> None:
        now = time.time()
        expired = [s for s, t in self._last_alert.items() if now - t > self._cooldown]
        for s in expired:
            del self._last_alert[s]

    @property
    def active_count(self) -> int:
        now = time.time()
        return sum(1 for t in self._last_alert.values() if now - t < self._cooldown)


class Scanner:

    def __init__(
        self,
        config: dict,
        binance: BinanceClient,
        notifier: TelegramNotifier,
        tracker: Optional[SignalTracker] = None,
        market_cap: Optional[MarketCapProvider] = None,
    ) -> None:
        sc = config["scanner"]

        self.timeframe:              str   = sc.get("timeframe", "1h")
        self.interval:               int   = sc.get("scan_interval_seconds", 900)
        self.brk_lookback:           int   = sc.get("breakout_lookback_candles", 24)
        self.consec_vol_candles:     int   = sc.get("consecutive_vol_candles", 3)
        self.max_price_chg_24h:      float = sc.get("max_price_change_24h_pct", 20.0)
        self.min_vol_usdt:           float = sc.get("min_volume_usdt", 0)
        self.cooldown_hours:         float = sc.get("cooldown_hours", 12)
        self.excluded:               set   = set(sc.get("excluded_symbols", []))

        self._candles_needed = max(self.brk_lookback + 1, self.consec_vol_candles, 20)

        self._binance = binance
        self._tg = notifier
        self._tracker = tracker
        self._market_cap = market_cap
        self._cooldown = _CooldownTracker(cooldown_seconds=self.cooldown_hours * 3600)
        self._mark_prices: Dict[str, float] = {}
        self._tickers: Dict[str, dict] = {}
        self._running = False

    @staticmethod
    def _fmt_vol_usd(vol: float) -> str:
        if vol >= 1e9:
            return f"${vol / 1e9:.1f}B"
        if vol >= 1e6:
            return f"${vol / 1e6:.2f}M"
        if vol >= 1e3:
            return f"${vol / 1e3:.0f}K"
        return f"${vol:.0f}"

    @staticmethod
    def _ema(values: List[float], period: int) -> float:
        """Calculate EMA for a list of values."""
        if len(values) < period:
            return 0.0
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        try:
            self._send_startup()
        except Exception as exc:
            logger.warning("Startup message failed (non-fatal): %s", exc)
        self._running = True
        logger.info(
            "Scanner loop started  (interval %ds, need %d candles/symbol, cooldown %.1fh)",
            self.interval, self._candles_needed, self.cooldown_hours,
        )
        while self._running:
            t0 = time.time()
            try:
                self._cycle()
            except Exception:
                logger.error("Scan cycle error — will retry next interval", exc_info=True)
            elapsed = time.time() - t0
            logger.info("Cycle finished in %.1fs", elapsed)
            self._sleep(max(0.0, self.interval - elapsed))

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    def _cycle(self) -> None:
        try:
            all_syms = self._binance.get_usdt_perpetual_symbols()
        except Exception as exc:
            logger.error("Failed to fetch symbol list — skipping cycle: %s", exc)
            return
        if not all_syms:
            logger.warning("Symbol list is empty — skipping cycle")
            return

        try:
            self._mark_prices = self._binance.get_mark_prices()
        except Exception as exc:
            logger.warning("Mark-price fetch failed: %s", exc)
            self._mark_prices = {}

        try:
            self._tickers = self._binance.get_24h_tickers()
        except Exception as exc:
            logger.warning("24h ticker fetch failed: %s", exc)
            self._tickers = {}

        targets = [
            s for s in all_syms
            if s["symbol"] not in self.excluded
            and not self._cooldown.is_on_cooldown(s["symbol"])
        ]

        logger.info(
            "Targets: %d / %d  (%d excluded, %d on cooldown)",
            len(targets), len(all_syms),
            len(self.excluded), self._cooldown.active_count,
        )

        alerts = 0
        for idx, sym in enumerate(targets, 1):
            if not self._running:
                return
            logger.info("Scanning [%d/%d] %s", idx, len(targets), sym["symbol"])
            try:
                data = self._analyse(sym)
                if data:
                    if self._tg.send_alert(data):
                        alerts += 1
                    time.sleep(0.3)
            except Exception:
                logger.error("Error analysing %s", sym["symbol"], exc_info=True)

        self._cooldown.prune()
        if alerts:
            logger.info("Alerts sent this cycle: %d", alerts)

    def _analyse(self, sym: dict) -> Optional[dict]:
        symbol = sym["symbol"]

        if self._cooldown.is_on_cooldown(symbol):
            return None

        candles = self._binance.get_closed_klines(
            symbol, self.timeframe, self._candles_needed,
        )
        if len(candles) < self._candles_needed:
            return None

        last = candles[-1]
        ticker = self._tickers.get(symbol, {})

        # ── FILTER 1: 24h high breakout ──────────────────────────────
        lookback_candles = candles[-(self.brk_lookback + 1):-1]
        if len(lookback_candles) < self.brk_lookback:
            return None

        high_24h = max(c["high"] for c in lookback_candles)
        if last["close"] <= high_24h:
            logger.info("%s  rejected — close %.8f did not break 24h high %.8f",
                        symbol, last["close"], high_24h)
            return None

        brk_margin_pct = ((last["close"] - high_24h) / high_24h) * 100
        logger.info("%s  ✅ Breakout +%.2f%% above 24h high %.8f",
                    symbol, brk_margin_pct, high_24h)

        # ── FILTER 2: consecutive volume increase (last 3 candles) ───
        consec = candles[-self.consec_vol_candles:]
        if len(consec) < self.consec_vol_candles:
            return None

        is_increasing = all(
            consec[i]["quote_volume"] > consec[i - 1]["quote_volume"]
            for i in range(1, len(consec))
        )
        if not is_increasing:
            vol_vals = [self._fmt_vol_usd(c["quote_volume"]) for c in consec]
            logger.info("%s  rejected — volume NOT consecutively increasing: %s",
                        symbol, " → ".join(vol_vals))
            return None

        vol_vals = [self._fmt_vol_usd(c["quote_volume"]) for c in consec]
        logger.info("%s  ✅ Consecutive volume: %s", symbol, " → ".join(vol_vals))

        # ── FILTER 3: 24h price change cap ───────────────────────────
        price_chg_24h = ticker.get("price_change_pct", 0)
        if abs(price_chg_24h) > self.max_price_chg_24h:
            logger.info("%s  rejected — 24h price change %.1f%% > max %.1f%%",
                        symbol, price_chg_24h, self.max_price_chg_24h)
            return None

        logger.info("%s  ✅ 24h price change: %.1f%%", symbol, price_chg_24h)

        # ── optional min volume floor ─────────────────────────────────
        current_vol = last["quote_volume"]
        if self.min_vol_usdt > 0 and current_vol < self.min_vol_usdt:
            logger.info("%s  rejected — volume %s < min %s",
                        symbol, self._fmt_vol_usd(current_vol),
                        self._fmt_vol_usd(self.min_vol_usdt))
            return None

        # ── ALL MAIN CRITERIA PASSED — collect additional data ───────
        additional = self._collect_additional(symbol, candles, last, ticker)

        self._cooldown.record(symbol)

        price = self._mark_prices.get(symbol)
        btc_price = self._mark_prices.get("BTCUSDT")
        candle_dt = datetime.fromtimestamp(last["open_time"] / 1000, tz=timezone.utc)
        now_dt = datetime.now(timezone.utc)

        vol_baseline = candles[-(20 + 1):-1]
        avg_baseline = sum(c["quote_volume"] for c in vol_baseline) / len(vol_baseline) if vol_baseline else 0
        rvol = current_vol / avg_baseline if avg_baseline > 0 else 0

        alert = {
            "symbol":            symbol,
            "timeframe":         self.timeframe,
            "price":             f"{price:.8f}" if price else "N/A",
            "price_change_24h":  price_chg_24h,
            "breakout_margin_pct": brk_margin_pct,
            "high_24h":          high_24h,
            "vol_candle_1":      consec[0]["quote_volume"],
            "vol_candle_2":      consec[1]["quote_volume"],
            "vol_candle_3":      consec[2]["quote_volume"],
            "vol_candle_1_fmt":  vol_vals[0],
            "vol_candle_2_fmt":  vol_vals[1],
            "vol_candle_3_fmt":  vol_vals[2],
            "rvol":              rvol,
            "btc_price":         btc_price,
            "candle_time":       candle_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "alert_time":        now_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "alert_time_ts":     now_dt.timestamp(),
            "cooldown_hours":    self.cooldown_hours,
            "additional_data":   additional,
        }

        if self._tracker:
            self._tracker.record_signal(alert)

        logger.info(
            "🚨 SIGNAL  %s  brk:+%.2f%%  vols:%s→%s→%s  24h:%.1f%%",
            symbol, brk_margin_pct,
            vol_vals[0], vol_vals[1], vol_vals[2], price_chg_24h,
        )
        return alert

    def _collect_additional(
        self, symbol: str, candles: list, last: dict, ticker: dict
    ) -> dict:
        """
        Collect additional context data. Each piece is wrapped in try/except
        so any failure does NOT block the signal from firing.
        """
        data: dict = {}

        # RVOL vs 20-candle baseline
        try:
            baseline = candles[-(20 + 1):-1]
            if baseline:
                avg_b = sum(c["quote_volume"] for c in baseline) / len(baseline)
                data["rvol_20"] = round(last["quote_volume"] / avg_b, 2) if avg_b > 0 else None
                data["vol_baseline_avg"] = round(avg_b, 2)
        except Exception:
            pass

        # OI: current vs average of last 24 periods
        try:
            oi_hist = self._binance.get_oi_history(symbol, "1h", 25)
            if len(oi_hist) >= 2:
                current_oi = oi_hist[-1]["oi_value_usdt"]
                prev_oi_values = [h["oi_value_usdt"] for h in oi_hist[:-1]]
                avg_oi = sum(prev_oi_values) / len(prev_oi_values)
                data["oi_current_usdt"] = round(current_oi, 2)
                data["oi_avg_24h_usdt"] = round(avg_oi, 2)
                data["oi_change_pct"] = round(((current_oi - avg_oi) / avg_oi) * 100, 2) if avg_oi > 0 else None
                # relative OI growth ratio
                if len(oi_hist) >= 3:
                    oi_changes = [
                        oi_hist[i]["oi_value_usdt"] - oi_hist[i - 1]["oi_value_usdt"]
                        for i in range(1, len(oi_hist))
                    ]
                    current_oi_growth = oi_changes[-1]
                    avg_oi_growth = sum(oi_changes[:-1]) / len(oi_changes[:-1]) if oi_changes[:-1] else 0
                    data["oi_growth_current"] = round(current_oi_growth, 2)
                    data["oi_growth_avg"] = round(avg_oi_growth, 2)
                    if avg_oi_growth != 0:
                        data["oi_growth_ratio"] = round(current_oi_growth / abs(avg_oi_growth), 2)
        except Exception:
            pass

        # Funding rate
        try:
            fr = self._binance.get_funding_rate(symbol)
            if fr is not None:
                data["funding_rate"] = round(fr * 100, 4)
                data["funding_in_ideal_range"] = -0.02 <= fr * 100 <= 0.15
        except Exception:
            pass

        # 24h volume liquidity
        try:
            vol_24h = ticker.get("quote_volume_24h", 0)
            data["vol_24h_usdt"] = round(vol_24h, 2)
            data["vol_24h_above_50m"] = vol_24h >= 50_000_000
        except Exception:
            pass

        # 4h EMA50
        try:
            candles_4h = self._binance.get_closed_klines(symbol, "4h", 55)
            if len(candles_4h) >= 50:
                closes_4h = [c["close"] for c in candles_4h]
                ema50 = self._ema(closes_4h, 50)
                current_price = last["close"]
                data["ema50_4h"] = round(ema50, 8)
                data["price_above_ema50_4h"] = current_price > ema50
                data["ema50_distance_pct"] = round(((current_price - ema50) / ema50) * 100, 2) if ema50 > 0 else None
        except Exception:
            pass

        # Volatility compression (range of last 10 candles vs prior 10)
        try:
            if len(candles) >= 20:
                recent_10 = candles[-10:]
                prior_10 = candles[-20:-10]

                def avg_range(cs):
                    return sum((c["high"] - c["low"]) / c["close"] * 100 for c in cs if c["close"] > 0) / len(cs)

                recent_range_pct = avg_range(recent_10)
                prior_range_pct = avg_range(prior_10)
                data["volatility_recent_10_pct"] = round(recent_range_pct, 4)
                data["volatility_prior_10_pct"] = round(prior_range_pct, 4)
                if prior_range_pct > 0:
                    compression_ratio = recent_range_pct / prior_range_pct
                    data["volatility_compression_ratio"] = round(compression_ratio, 3)
                    data["is_compressed"] = compression_ratio < 0.7
        except Exception:
            pass

        # Market cap from CoinGecko (optional)
        try:
            if self._market_cap is not None:
                base = symbol.replace("USDT", "").replace("BUSD", "")
                mcap = self._market_cap.get(base)
                data["market_cap_usd"] = mcap
                data["market_cap_fmt"] = self._market_cap.format(base)
        except Exception:
            pass

        return data

    def _send_startup(self) -> None:
        lines = [
            "⚙️ <b>Scanner Started — New Strategy</b>",
            "",
            "<b>Main Criteria (all 3 must pass):</b>",
            f"1️⃣ Current 1h candle closes above last {self.brk_lookback}h high",
            f"2️⃣ Last {self.consec_vol_candles} candles have strictly increasing volume",
            f"3️⃣ 24h price change ≤ ±{self.max_price_chg_24h}%",
            "",
            "<b>Additional data collected (not filters):</b>",
            "📊 RVOL vs 20-candle baseline",
            "📈 OI change vs 24h average",
            "💰 Funding rate",
            "💧 24h volume (liquidity)",
            "📉 Price vs 4h EMA50",
            "🔲 Volatility compression score",
            "",
            f"⏱ Scan every {self.interval}s  |  Cooldown {self.cooldown_hours}h",
        ]
        if self.min_vol_usdt > 0:
            lines.append(f"🔻 Min volume: {self._fmt_vol_usd(self.min_vol_usdt)}")
        self._tg.send_startup("\n".join(lines))
