"""
Core scanner engine.

Analysis flow per symbol:
  1. Cooldown check
  2. Volume spike detection
  3. Candle quality filters (bullish, wick, body)
  4. Trend strength filter
  5. Breakout confirmation + margin limits (optional)
  6. Open interest surge (optional)
  7. Enrichment + alert + track
"""

from __future__ import annotations

import logging
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
    ) -> None:
        sc = config["scanner"]

        # volume
        self.timeframe:       str   = sc["timeframe"]
        self.interval:        int   = sc["scan_interval_seconds"]
        self.mcap_max:        float = sc["market_cap_max_usd"]
        self.vol_recent:      int   = sc["volume_recent_candles"]
        self.vol_baseline:    int   = sc["volume_baseline_candles"]
        self.vol_mult:        float = sc["volume_multiplier"]

        # breakout
        self.brk_on:          bool  = sc["breakout_enabled"]
        self.brk_lookback:    int   = sc["breakout_lookback"]
        self.min_brk_margin:  float = sc.get("min_breakout_margin_pct", 0)
        self.max_brk_margin:  float = sc.get("max_breakout_margin_pct", 0)

        # open interest
        self.oi_on:           bool  = sc["open_interest_enabled"]
        self.oi_periods:      int   = sc["open_interest_periods"]
        self.oi_min_pct:      float = sc["open_interest_min_increase_pct"]

        # candle quality
        self.bullish_required: bool  = sc.get("bullish_candle_required", True)
        self.max_wick_pct:     float = sc.get("max_upper_wick_pct", 0)
        self.min_body_pct:     float = sc.get("min_body_pct", 0)

        # trend
        self.trend_count:      int   = sc.get("trend_candles", 5)
        self.min_trend_pct:    float = sc.get("min_trend_green_pct", 0)

        self.excluded:        set   = set(sc.get("excluded_symbols", []))
        self.cooldown_hours:  float = sc.get("cooldown_hours", 12)

        # new strategy filters
        self.rvol_threshold:      float = sc.get("rvol_threshold", 0)
        self.min_vol_usdt:        float = sc.get("min_volume_usdt", 0)
        self.consec_vol_candles:  int   = sc.get("consecutive_volume_candles", 0)
        self.max_price_chg_24h:   float = sc.get("max_price_change_24h_pct", 0)
        self.weekly_vol_mult:     float = sc.get("weekly_volume_multiplier", 0)

        # candles needed
        vol_need = self.vol_recent + self.vol_baseline
        brk_need = (self.brk_lookback + 1) if self.brk_on else 0
        self._candles_needed = max(vol_need, brk_need, self.trend_count, self.consec_vol_candles)

        # components
        self._binance = binance
        self._tg = notifier
        rl = config.get("rate_limit", {})
        self._mcap = MarketCapProvider(
            cache_minutes=rl.get("market_cap_cache_minutes", 120),
            include_unknown=sc.get("include_unknown_market_cap", True),
        )
        self._cooldown = _CooldownTracker(
            cooldown_seconds=self.cooldown_hours * 3600,
        )
        self._tracker = tracker
        self._mark_prices: Dict[str, float] = {}
        self._tickers: Dict[str, dict] = {}
        self._running = False

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _candle_metrics(candle: dict) -> dict:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        rng = h - l
        if rng <= 0:
            return {"color": "DOJI", "body_pct": 0.0, "upper_wick_pct": 0.0, "lower_wick_pct": 0.0}
        body = abs(c - o)
        upper_wick = h - max(c, o)
        lower_wick = min(c, o) - l
        return {
            "color": "GREEN" if c >= o else "RED",
            "body_pct": round((body / rng) * 100, 1),
            "upper_wick_pct": round((upper_wick / rng) * 100, 1),
            "lower_wick_pct": round((lower_wick / rng) * 100, 1),
        }

    @staticmethod
    def _trend_strength(candles: List[dict], count: int) -> dict:
        recent = candles[-count:] if len(candles) >= count else candles
        pattern = ""
        greens = 0
        for c in recent:
            if c["close"] > c["open"]:
                pattern += "G"
                greens += 1
            else:
                pattern += "R"
        return {"green_count": greens, "total": len(recent), "pattern": pattern}

    @staticmethod
    def _fmt_vol_usd(vol: float) -> str:
        if vol >= 1e9:
            return f"${vol / 1e9:.1f}B"
        if vol >= 1e6:
            return f"${vol / 1e6:.1f}M"
        if vol >= 1e3:
            return f"${vol / 1e3:.0f}K"
        return f"${vol:.0f}"

    # ── lifecycle ────────────────────────────────────────────────────

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

    # ── scan cycle ───────────────────────────────────────────────────

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
            and self._mcap.passes_filter(s["base_asset"], self.mcap_max)
        ]
        logger.info(
            "Targets: %d / %d  (mcap ≤ $%.0fM, %d excluded, %d on cooldown)",
            len(targets), len(all_syms),
            self.mcap_max / 1e6, len(self.excluded),
            self._cooldown.active_count,
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

    # ── per-symbol analysis ──────────────────────────────────────────

    def _analyse(self, sym: dict) -> Optional[dict]:
        symbol = sym["symbol"]
        base   = sym["base_asset"]

        if self._cooldown.is_on_cooldown(symbol):
            return None

        candles = self._binance.get_closed_klines(
            symbol, self.timeframe, self._candles_needed,
        )
        if len(candles) < self._candles_needed:
            return None

        last = candles[-1]

        # ── 1. volume check ──────────────────────────────────────────
        recent   = candles[-self.vol_recent:]
        baseline = candles[-(self.vol_recent + self.vol_baseline):-self.vol_recent]

        avg_r = sum(c["quote_volume"] for c in recent) / len(recent)
        avg_b = sum(c["quote_volume"] for c in baseline) / len(baseline)

        if avg_b <= 0:
            return None
        ratio = avg_r / avg_b
        if ratio < self.vol_mult:
            return None

        # ── 1a. minimum absolute volume (avoid illiquid coins) ────────
        if self.min_vol_usdt > 0 and avg_r < self.min_vol_usdt:
            logger.debug(
                "%s  rejected — vol %s < min %s",
                symbol, self._fmt_vol_usd(avg_r), self._fmt_vol_usd(self.min_vol_usdt),
            )
            return None

        # ── 1b. RVOL threshold (bonus powerful filter) ────────────────
        if self.rvol_threshold > 0 and ratio < self.rvol_threshold:
            logger.debug(
                "%s  rejected — RVOL %.2fx < threshold %.2fx", symbol, ratio, self.rvol_threshold,
            )
            return None

        # ── 1c. consecutive volume growth (accumulation pattern) ──────
        if self.consec_vol_candles > 1:
            consec = candles[-self.consec_vol_candles:]
            if len(consec) >= self.consec_vol_candles:
                is_increasing = all(
                    consec[i]["quote_volume"] > consec[i - 1]["quote_volume"]
                    for i in range(1, len(consec))
                )
                if not is_increasing:
                    logger.debug("%s  rejected — volume not consecutively increasing", symbol)
                    return None

        # ── 1d. 24h price change cap (avoid late entries) ─────────────
        ticker = self._tickers.get(symbol, {})
        price_chg_24h = ticker.get("price_change_pct", 0)
        if self.max_price_chg_24h > 0 and abs(price_chg_24h) > self.max_price_chg_24h:
            logger.debug(
                "%s  rejected — 24h price change %.1f%% > max %.1f%%",
                symbol, price_chg_24h, self.max_price_chg_24h,
            )
            return None

        logger.info("%s  volume spike %.2fx (RVOL)  24h chg: %.1f%%", symbol, ratio, price_chg_24h)

        # ── 2. candle quality ────────────────────────────────────────
        metrics = self._candle_metrics(last)

        if self.bullish_required and metrics["color"] != "GREEN":
            logger.debug("%s  rejected — RED candle", symbol)
            return None

        if self.max_wick_pct > 0 and metrics["upper_wick_pct"] > self.max_wick_pct:
            logger.debug(
                "%s  rejected — wick %.1f%% > max %.1f%%",
                symbol, metrics["upper_wick_pct"], self.max_wick_pct,
            )
            return None

        if self.min_body_pct > 0 and metrics["body_pct"] < self.min_body_pct:
            logger.debug(
                "%s  rejected — body %.1f%% < min %.1f%%",
                symbol, metrics["body_pct"], self.min_body_pct,
            )
            return None

        # ── 3. trend strength ────────────────────────────────────────
        trend = self._trend_strength(candles, self.trend_count)

        if self.min_trend_pct > 0 and trend["total"] > 0:
            green_pct = (trend["green_count"] / trend["total"]) * 100
            if green_pct < self.min_trend_pct:
                logger.debug(
                    "%s  rejected — trend %.0f%% green < min %.0f%%",
                    symbol, green_pct, self.min_trend_pct,
                )
                return None

        logger.info(
            "%s  candle OK — %s body:%.0f%% wick:%.0f%% trend:%d/%d",
            symbol, metrics["color"], metrics["body_pct"],
            metrics["upper_wick_pct"], trend["green_count"], trend["total"],
        )

        # ── 4. breakout check (optional) ─────────────────────────────
        brk_ok: Optional[bool] = None
        brk_level: Optional[float] = None
        brk_margin: Optional[float] = None

        if self.brk_on:
            lookback = candles[-(self.brk_lookback + 1):-1]
            if len(lookback) < self.brk_lookback:
                return None
            brk_level = max(c["high"] for c in lookback)
            brk_ok = last["close"] > brk_level
            if not brk_ok:
                logger.debug("%s  breakout NOT confirmed", symbol)
                return None

            brk_margin = ((last["close"] - brk_level) / brk_level) * 100

            # too small breakout — barely broke out
            if self.min_brk_margin > 0 and brk_margin < self.min_brk_margin:
                logger.debug(
                    "%s  rejected — breakout margin %.2f%% < min %.2f%%",
                    symbol, brk_margin, self.min_brk_margin,
                )
                return None

            # too big breakout — buying the top
            if self.max_brk_margin > 0 and brk_margin > self.max_brk_margin:
                logger.debug(
                    "%s  rejected — breakout margin %.2f%% > max %.2f%% (buying the top)",
                    symbol, brk_margin, self.max_brk_margin,
                )
                return None

            logger.info(
                "%s  breakout confirmed +%.2f%% above %.6f",
                symbol, brk_margin, brk_level,
            )

        # ── 5. open-interest check (optional) ────────────────────────
        oi_pct: Optional[float] = None
        if self.oi_on:
            oi_pct = self._oi_change(symbol)
            if oi_pct is None:
                logger.debug("%s  OI data unavailable — skipping", symbol)
                return None
            if oi_pct < self.oi_min_pct:
                logger.debug(
                    "%s  OI +%.2f%% < threshold %.2f%%",
                    symbol, oi_pct, self.oi_min_pct,
                )
                return None
            logger.info("%s  OI +%.2f%%", symbol, oi_pct)

        # ── 6. weekly liquidity expansion check ──────────────────────
        weekly_rvol: Optional[float] = None
        if self.weekly_vol_mult > 0:
            daily = self._binance.get_closed_klines(symbol, "1d", 7)
            if len(daily) >= 7:
                weekly_avg = sum(c["quote_volume"] for c in daily) / len(daily)
                vol_24h = ticker.get("quote_volume_24h", 0)
                if weekly_avg > 0:
                    weekly_rvol = vol_24h / weekly_avg
                    if weekly_rvol < self.weekly_vol_mult:
                        logger.debug(
                            "%s  rejected — 24h vol %.1fx weekly avg (need %.1fx)",
                            symbol, weekly_rvol, self.weekly_vol_mult,
                        )
                        return None
                    logger.info("%s  weekly liquidity expansion %.1fx", symbol, weekly_rvol)

        # ── 7. all passed — build alert ──────────────────────────────
        self._cooldown.record(symbol)

        price = self._mark_prices.get(symbol)
        btc_price = self._mark_prices.get("BTCUSDT")
        candle_dt = datetime.fromtimestamp(last["open_time"] / 1000, tz=timezone.utc)
        now_dt = datetime.now(timezone.utc)

        alert = {
            "symbol":              symbol,
            "timeframe":           self.timeframe,
            "mcap":                self._mcap.format(base),
            "price":               f"{price:.8f}" if price else "N/A",
            "vol_ratio":           ratio,
            "vol_threshold":       self.vol_mult,
            "recent_vol_usdt":     avg_r,
            "baseline_vol_usdt":   avg_b,
            "recent_vol_fmt":      self._fmt_vol_usd(avg_r),
            "baseline_vol_fmt":    self._fmt_vol_usd(avg_b),
            "price_change_24h":    price_chg_24h,
            "weekly_rvol":         weekly_rvol,
            "candle_color":        metrics["color"],
            "body_pct":            metrics["body_pct"],
            "upper_wick_pct":      metrics["upper_wick_pct"],
            "lower_wick_pct":      metrics["lower_wick_pct"],
            "breakout_enabled":    self.brk_on,
            "breakout_confirmed":  brk_ok,
            "breakout_level":      brk_level,
            "breakout_margin_pct": brk_margin,
            "oi_enabled":          self.oi_on,
            "oi_pct":              oi_pct,
            "trend_green":         trend["green_count"],
            "trend_total":         trend["total"],
            "trend_pattern":       trend["pattern"],
            "btc_price":           btc_price,
            "candle_time":         candle_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "alert_time":          now_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "cooldown_hours":      self.cooldown_hours,
        }

        if self._tracker:
            self._tracker.record_signal(alert)

        logger.info(
            "🚨  ALERT  %s  vol=%.2fx  %s  body:%.0f%%  wick:%.0f%%  "
            "brk:%s  oi:%s  trend:%d/%d",
            symbol, ratio, metrics["color"], metrics["body_pct"],
            metrics["upper_wick_pct"], brk_margin, oi_pct,
            trend["green_count"], trend["total"],
        )
        return alert

    # ── OI helper ────────────────────────────────────────────────────

    def _oi_change(self, symbol: str) -> Optional[float]:
        hist = self._binance.get_oi_history(
            symbol, self.timeframe, self.oi_periods + 1,
        )
        if len(hist) < self.oi_periods + 1:
            return None
        cur  = hist[-1]["oi_value_usdt"]
        prev = [h["oi_value_usdt"] for h in hist[:-1]]
        avg  = sum(prev) / len(prev)
        if avg <= 0:
            return None
        return ((cur - avg) / avg) * 100.0

    def _candle_ms(self) -> int:
        multipliers = {
            "m": 60_000, "h": 3_600_000,
            "d": 86_400_000, "w": 604_800_000,
        }
        tf = self.timeframe
        for suffix, ms in multipliers.items():
            if tf.endswith(suffix):
                return int(tf[:-len(suffix)]) * ms
        return 3_600_000

    def _send_startup(self) -> None:
        lines = [
            "⚙️ <b>Configuration</b>",
            f"• Timeframe: {self.timeframe}",
            f"• Market-cap: ≤ ${self.mcap_max / 1e6:.0f}M",
            f"• Volume: last {self.vol_recent} vs prev {self.vol_baseline} (≥{self.vol_mult}x RVOL"
            + (f", bonus≥{self.rvol_threshold}x" if self.rvol_threshold > 0 else "") + ")",
            f"• Min volume: {self._fmt_vol_usd(self.min_vol_usdt) if self.min_vol_usdt > 0 else 'OFF'}",
            f"• Consec. vol growth: {self.consec_vol_candles} candles" if self.consec_vol_candles > 1 else "• Consec. vol growth: OFF",
            f"• 24h price change cap: ≤{self.max_price_chg_24h}%" if self.max_price_chg_24h > 0 else "• 24h price change cap: OFF",
            f"• Weekly liquidity: ≥{self.weekly_vol_mult}x weekly avg" if self.weekly_vol_mult > 0 else "• Weekly liquidity: OFF",
            f"• Candle: bullish={'ON' if self.bullish_required else 'OFF'}"
            f"  body≥{self.min_body_pct}%  wick≤{self.max_wick_pct}%",
            f"• Trend: ≥{self.min_trend_pct:.0f}% green ({self.trend_count} candles)",
        ]
        if self.brk_on:
            lines.append(
                f"• Breakout: <b>ON</b>  lookback:{self.brk_lookback}"
                f"  margin:{self.min_brk_margin}-{self.max_brk_margin}%"
            )
        else:
            lines.append("• Breakout: <b>OFF</b>")
        if self.oi_on:
            lines.append(f"• OI: <b>ON</b>  ≥{self.oi_min_pct}%  periods:{self.oi_periods}")
        else:
            lines.append("• OI: <b>OFF</b>")
        lines.append(f"• Cooldown: {self.cooldown_hours}h  |  Tracker: {'ON' if self._tracker else 'OFF'}")
        lines.append(f"• Scan interval: {self.interval}s")
        self._tg.send_startup("\n".join(lines))