"""
Core scanner engine.

Responsibilities
────────────────
1. Iterate over all USDT perpetual symbols that pass the market-cap filter.
2. For each symbol fetch closed klines and evaluate:
   a) Volume spike  (recent avg vs baseline avg)
   b) Breakout      (close > highest high of N prior candles)   [optional]
   c) OI surge      (current OI vs avg of N prior periods)       [optional]
3. Enforce a configurable cooldown per symbol (default 12 h) so the same
   coin is not alerted repeatedly.
4. Dispatch matching alerts to Telegram.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from binance_client import BinanceClient
from market_cap import MarketCapProvider
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)


# ── cooldown tracker ─────────────────────────────────────────────────

class _CooldownTracker:
    """
    Enforce a time-based cooldown per symbol.

    After an alert fires for a symbol, that symbol is blocked for
    *cooldown_seconds*.  This prevents spam when a coin stays in
    a high-volume regime across many consecutive candles.
    """

    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown = cooldown_seconds
        self._last_alert: Dict[str, float] = {}      # symbol → epoch

    def is_on_cooldown(self, symbol: str) -> bool:
        last = self._last_alert.get(symbol)
        if last is None:
            return False
        remaining = self._cooldown - (time.time() - last)
        if remaining > 0:
            logger.debug(
                "%s  on cooldown — %.1f min remaining",
                symbol, remaining / 60,
            )
            return True
        return False

    def record(self, symbol: str) -> None:
        self._last_alert[symbol] = time.time()

    def remaining_str(self, symbol: str) -> str:
        """Human-readable time until cooldown expires."""
        last = self._last_alert.get(symbol)
        if last is None:
            return "0s"
        rem = self._cooldown - (time.time() - last)
        if rem <= 0:
            return "0s"
        hours = int(rem // 3600)
        mins = int((rem % 3600) // 60)
        return f"{hours}h {mins}m"

    def prune(self) -> None:
        """Drop expired entries to keep memory bounded."""
        now = time.time()
        expired = [
            s for s, t in self._last_alert.items()
            if now - t > self._cooldown
        ]
        for s in expired:
            del self._last_alert[s]

    @property
    def active_count(self) -> int:
        now = time.time()
        return sum(1 for t in self._last_alert.values() if now - t < self._cooldown)


# ── main scanner class ──────────────────────────────────────────────

class Scanner:

    def __init__(self, config: dict) -> None:
        sc = config["scanner"]

        # tunables
        self.timeframe:       str   = sc["timeframe"]
        self.interval:        int   = sc["scan_interval_seconds"]
        self.mcap_max:        float = sc["market_cap_max_usd"]
        self.vol_recent:      int   = sc["volume_recent_candles"]
        self.vol_baseline:    int   = sc["volume_baseline_candles"]
        self.vol_mult:        float = sc["volume_multiplier"]
        self.brk_on:          bool  = sc["breakout_enabled"]
        self.brk_lookback:    int   = sc["breakout_lookback"]
        self.oi_on:           bool  = sc["open_interest_enabled"]
        self.oi_periods:      int   = sc["open_interest_periods"]
        self.oi_min_pct:      float = sc["open_interest_min_increase_pct"]
        self.excluded:        set   = set(sc.get("excluded_symbols", []))
        self.cooldown_hours:  float = sc.get("cooldown_hours", 12)

        # how many closed candles we need per symbol
        vol_need = self.vol_recent + self.vol_baseline
        brk_need = (self.brk_lookback + 1) if self.brk_on else 0
        self._candles_needed = max(vol_need, brk_need)

        # components
        rl = config.get("rate_limit", {})
        self._binance = BinanceClient(
            api_key=config["binance"].get("api_key", ""),
            api_secret=config["binance"].get("api_secret", ""),
            delay_ms=rl.get("binance_delay_ms", 100),
        )
        self._mcap = MarketCapProvider(
            cache_minutes=rl.get("market_cap_cache_minutes", 60),
            include_unknown=sc.get("include_unknown_market_cap", True),
        )
        self._tg = TelegramNotifier(
            bot_token=config["telegram"]["bot_token"],
            chat_id=config["telegram"]["chat_id"],
        )
        self._cooldown = _CooldownTracker(
            cooldown_seconds=self.cooldown_hours * 3600,
        )
        self._mark_prices: Dict[str, float] = {}
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        if not self._tg.validate():
            logger.error("Telegram validation failed — aborting.")
            return

        self._send_startup()
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
                logger.error("Scan cycle error", exc_info=True)
            elapsed = time.time() - t0
            logger.info("Cycle finished in %.1fs", elapsed)
            self._sleep(max(0.0, self.interval - elapsed))

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    # ── one full scan cycle ──────────────────────────────────────────

    def _cycle(self) -> None:
        # 1 — symbols
        all_syms = self._binance.get_usdt_perpetual_symbols()

        # 2 — mark prices (one call)
        try:
            self._mark_prices = self._binance.get_mark_prices()
        except Exception as exc:
            logger.warning("Mark-price fetch failed: %s", exc)
            self._mark_prices = {}

        # 3 — market-cap + exclusion filter
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

        # 4 — analyse each target
        alerts = 0
        for idx, sym in enumerate(targets, 1):
            if not self._running:
                return
            try:
                data = self._analyse(sym)
                if data:
                    if self._tg.send_alert(data):
                        alerts += 1
                    time.sleep(0.3)
            except Exception:
                logger.error("Error analysing %s", sym["symbol"], exc_info=True)
            if idx % 50 == 0:
                logger.debug("Progress %d / %d", idx, len(targets))

        # 5 — prune expired cooldowns to free memory
        self._cooldown.prune()

        if alerts:
            logger.info("Alerts sent this cycle: %d", alerts)

    # ── per-symbol analysis ──────────────────────────────────────────

    def _analyse(self, sym: dict) -> Optional[dict]:
        symbol = sym["symbol"]
        base   = sym["base_asset"]

        # ── cooldown check (skip early to save API calls) ────────────
        if self._cooldown.is_on_cooldown(symbol):
            return None

        candles = self._binance.get_closed_klines(
            symbol, self.timeframe, self._candles_needed,
        )
        if len(candles) < self._candles_needed:
            logger.debug(
                "%s: not enough candles (%d/%d)",
                symbol, len(candles), self._candles_needed,
            )
            return None

        last = candles[-1]

        # ── volume check ─────────────────────────────────────────────
        recent   = candles[-self.vol_recent:]
        baseline = candles[-(self.vol_recent + self.vol_baseline):-self.vol_recent]

        avg_r = sum(c["quote_volume"] for c in recent) / len(recent)
        avg_b = sum(c["quote_volume"] for c in baseline) / len(baseline)

        if avg_b <= 0:
            return None

        ratio = avg_r / avg_b
        if ratio < self.vol_mult:
            return None

        logger.info("%s  volume spike %.2fx", symbol, ratio)

        # ── breakout check (optional) ────────────────────────────────
        brk_ok: Optional[bool] = None
        if self.brk_on:
            lookback = candles[-(self.brk_lookback + 1):-1]
            if len(lookback) < self.brk_lookback:
                return None
            highest = max(c["high"] for c in lookback)
            brk_ok = last["close"] > highest
            if not brk_ok:
                logger.debug(
                    "%s  breakout NOT confirmed (close %.6f ≤ high %.6f)",
                    symbol, last["close"], highest,
                )
                return None
            logger.info("%s  breakout confirmed", symbol)

        # ── open-interest check (optional) ───────────────────────────
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

        # ── all conditions passed — record cooldown ──────────────────
        self._cooldown.record(symbol)

        price = self._mark_prices.get(symbol)
        candle_dt = datetime.fromtimestamp(last["open_time"] / 1000, tz=timezone.utc)
        now_dt    = datetime.now(timezone.utc)

        alert = {
            "symbol":             symbol,
            "timeframe":          self.timeframe,
            "mcap":               self._mcap.format(base),
            "vol_ratio":          ratio,
            "vol_threshold":      self.vol_mult,
            "breakout_enabled":   self.brk_on,
            "breakout_confirmed": brk_ok,
            "oi_enabled":         self.oi_on,
            "oi_pct":             oi_pct,
            "price":              f"{price:.6f}" if price else "N/A",
            "candle_time":        candle_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "alert_time":         now_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "cooldown_hours":     self.cooldown_hours,
        }
        logger.info(
            "🚨  ALERT  %s  vol=%.2fx  brk=%s  oi=%s  (cooldown %.0fh starts now)",
            symbol, ratio, brk_ok, oi_pct, self.cooldown_hours,
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

    # ── utilities ────────────────────────────────────────────────────

    def _candle_ms(self) -> int:
        """Approximate candle duration in milliseconds."""
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
            f"⚙️ <b>Configuration</b>",
            f"• Timeframe: {self.timeframe}",
            f"• Market-cap filter: ≤ ${self.mcap_max / 1e6:.0f}M",
            f"• Volume: last {self.vol_recent} vs prev {self.vol_baseline} (≥{self.vol_mult}x)",
        ]
        if self.brk_on:
            lines.append(f"• Breakout: <b>ON</b>  (lookback {self.brk_lookback})")
        else:
            lines.append("• Breakout: <b>OFF</b>")
        if self.oi_on:
            lines.append(
                f"• OI filter: <b>ON</b>  (≥{self.oi_min_pct}%, periods {self.oi_periods})"
            )
        else:
            lines.append("• OI filter: <b>OFF</b>")
        lines.append(f"• Cooldown: <b>{self.cooldown_hours}h</b> per symbol")
        lines.append(f"• Scan interval: {self.interval}s")
        self._tg.send_startup("\n".join(lines))