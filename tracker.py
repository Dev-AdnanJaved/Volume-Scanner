"""
Signal performance tracker with take-profit alerts.

Responsibilities:
  - Store every alert to disk with full enrichment
  - Continuously track highest price
  - Send take-profit target alerts when price hits configurable levels
  - Send reversal warnings when price drops significantly from peak
  - Archive signals after configurable max age
"""

from __future__ import annotations

import json
import logging
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from binance_client import BinanceClient
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class SignalTracker:

    def __init__(
        self,
        config: dict,
        binance: BinanceClient,
        notifier: TelegramNotifier,
    ) -> None:
        tc = config.get("tracker", {})
        self._max_age = tc.get("max_age_hours", 72) * 3600
        self._update_interval = tc.get("price_update_interval_seconds", 300)
        self._data_dir = Path(tc.get("data_dir", "data"))
        self._signals_file = self._data_dir / "signals.json"
        self._history_file = self._data_dir / "history.json"

        # take-profit settings
        self._tp_targets: List[int] = sorted(tc.get("take_profit_targets", [3, 5, 10, 15, 20]))
        self._reversal_enabled: bool = tc.get("reversal_alert_enabled", True)
        self._min_reversal_peak: float = tc.get("min_reversal_peak_pct", 3.0)
        self._reversal_drop: float = tc.get("reversal_drop_from_peak_pct", 5.0)

        self._binance = binance
        self._notifier = notifier
        self._lock = threading.Lock()
        self._running = False

        self._data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Tracker initialised  (max_age=%dh, update=%ds, TP targets=%s, reversal=%s)",
            self._max_age // 3600, self._update_interval,
            self._tp_targets, self._reversal_enabled,
        )

    # ── file I/O ─────────────────────────────────────────────────────

    def _load(self, path: Path) -> list:
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError) as exc:
            logger.error("Failed to read %s: %s", path, exc)
            return []

    def _save(self, path: Path, data: list) -> None:
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            tmp.replace(path)
        except IOError as exc:
            logger.error("Failed to write %s: %s", path, exc)

    # ── age formatting ───────────────────────────────────────────────

    @staticmethod
    def _fmt_age(ts: float) -> str:
        age = time.time() - ts
        if age < 3600:
            return f"{int(age / 60)}m"
        hours = int(age // 3600)
        mins = int((age % 3600) // 60)
        return f"{hours}h {mins}m"

    # ── record new signal ────────────────────────────────────────────

    def record_signal(self, alert: dict) -> None:
        try:
            price = float(alert["price"]) if alert.get("price") not in (None, "N/A") else 0.0
        except (ValueError, TypeError):
            price = 0.0

        signal = {
            "symbol":              alert["symbol"],
            "entry_price":         price,
            "highest_price":       price,
            "current_price":       price,
            "alert_time_ts":       time.time(),
            "alert_time":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "timeframe":           alert.get("timeframe", "1h"),
            "mcap":                alert.get("mcap", "Unknown"),
            "vol_ratio":           alert.get("vol_ratio", 0),
            "recent_vol_usdt":     alert.get("recent_vol_usdt", 0),
            "baseline_vol_usdt":   alert.get("baseline_vol_usdt", 0),
            "recent_vol_fmt":      alert.get("recent_vol_fmt", "N/A"),
            "baseline_vol_fmt":    alert.get("baseline_vol_fmt", "N/A"),
            "candle_color":        alert.get("candle_color", "N/A"),
            "body_pct":            alert.get("body_pct", 0),
            "upper_wick_pct":      alert.get("upper_wick_pct", 0),
            "lower_wick_pct":      alert.get("lower_wick_pct", 0),
            "breakout_confirmed":  alert.get("breakout_confirmed"),
            "breakout_level":      alert.get("breakout_level"),
            "breakout_margin_pct": alert.get("breakout_margin_pct"),
            "oi_pct":              alert.get("oi_pct"),
            "trend_green":         alert.get("trend_green", 0),
            "trend_total":         alert.get("trend_total", 0),
            "trend_pattern":       alert.get("trend_pattern", ""),
            "btc_price":           alert.get("btc_price"),
            # take-profit tracking
            "tp_sent":             [],
            "reversal_warned":     False,
        }

        with self._lock:
            signals = self._load(self._signals_file)
            signals.append(signal)
            self._save(self._signals_file, signals)

        logger.info("Tracker: recorded %s @ $%.8f", signal["symbol"], price)

    # ── price updates ────────────────────────────────────────────────

    def apply_prices(self, prices: Dict[str, float]) -> None:
        with self._lock:
            signals = self._load(self._signals_file)
            if not signals:
                return
            changed = False
            now = time.time()
            for sig in signals:
                sym = sig["symbol"]
                if sym not in prices:
                    continue
                current = prices[sym]
                sig["current_price"] = current
                sig["last_update_ts"] = now
                if current > sig.get("highest_price", 0):
                    sig["highest_price"] = current
                changed = True
            if changed:
                self._save(self._signals_file, signals)

    def fetch_and_apply(self) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self.apply_prices(prices)
        except Exception as exc:
            logger.warning("Tracker price update failed: %s", exc)

    # ── take-profit checking ─────────────────────────────────────────

    def _check_take_profits(self) -> None:
        """Check all active signals for TP targets and reversal conditions."""
        with self._lock:
            signals = self._load(self._signals_file)
            if not signals:
                return

            changed = False
            alerts_to_send: list[dict] = []

            for sig in signals:
                entry = sig.get("entry_price", 0)
                if entry <= 0:
                    continue

                highest = sig.get("highest_price", entry)
                current = sig.get("current_price", entry)
                high_pct = ((highest - entry) / entry) * 100
                cur_pct = ((current - entry) / entry) * 100
                age_str = self._fmt_age(sig["alert_time_ts"])

                # ensure tp_sent exists (backwards compat)
                tp_sent: list = sig.get("tp_sent", [])

                # ── check each TP target ─────────────────────────────
                for target in self._tp_targets:
                    if target in tp_sent:
                        continue
                    if high_pct >= target:
                        tp_sent.append(target)
                        changed = True
                        alerts_to_send.append({
                            "type":          "take_profit",
                            "symbol":        sig["symbol"],
                            "target":        target,
                            "entry_price":   entry,
                            "current_price": current,
                            "highest_price": highest,
                            "cur_pct":       cur_pct,
                            "high_pct":      high_pct,
                            "age_str":       age_str,
                        })
                        logger.info(
                            "🎯 TP target +%d%% hit for %s (peak: +%.2f%%, now: %+.2f%%)",
                            target, sig["symbol"], high_pct, cur_pct,
                        )

                sig["tp_sent"] = tp_sent

                # ── check reversal warning ───────────────────────────
                if (
                    self._reversal_enabled
                    and not sig.get("reversal_warned", False)
                    and high_pct >= self._min_reversal_peak
                ):
                    drop_from_peak = high_pct - cur_pct
                    if drop_from_peak >= self._reversal_drop:
                        sig["reversal_warned"] = True
                        changed = True
                        alerts_to_send.append({
                            "type":          "reversal",
                            "symbol":        sig["symbol"],
                            "entry_price":   entry,
                            "current_price": current,
                            "highest_price": highest,
                            "cur_pct":       cur_pct,
                            "high_pct":      high_pct,
                            "drop_pct":      drop_from_peak,
                            "age_str":       age_str,
                        })
                        logger.info(
                            "⚠️ Reversal warning for %s (peak: +%.2f%%, now: %+.2f%%, drop: %.2f%%)",
                            sig["symbol"], high_pct, cur_pct, drop_from_peak,
                        )

            if changed:
                self._save(self._signals_file, signals)

        # send alerts outside the lock
        for alert in alerts_to_send:
            try:
                if alert["type"] == "take_profit":
                    self._notifier.send_take_profit(alert)
                elif alert["type"] == "reversal":
                    self._notifier.send_reversal_warning(alert)
                time.sleep(0.5)
            except Exception as exc:
                logger.error("Failed to send %s alert: %s", alert["type"], exc)

    # ── archive expired ──────────────────────────────────────────────

    def archive_expired(self) -> int:
        now = time.time()
        with self._lock:
            signals = self._load(self._signals_file)
            history = self._load(self._history_file)

            active = []
            archived = 0

            for sig in signals:
                age = now - sig["alert_time_ts"]
                if age >= self._max_age:
                    entry = sig.get("entry_price", 0)
                    highest = sig.get("highest_price", 0)
                    current = sig.get("current_price", 0)
                    sig["archived_time_ts"] = now
                    sig["archived_time"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    sig["tracked_hours"] = round(age / 3600, 1)
                    if entry > 0:
                        sig["highest_pct"] = round(((highest - entry) / entry) * 100, 2)
                        sig["exit_pct"] = round(((current - entry) / entry) * 100, 2)
                        sig["exit_price"] = current
                    history.append(sig)
                    archived += 1
                else:
                    active.append(sig)

            if archived > 0:
                self._save(self._signals_file, active)
                self._save(self._history_file, history)

        return archived

    # ── data access ──────────────────────────────────────────────────

    def get_active_signals(self) -> List[dict]:
        now = time.time()
        with self._lock:
            signals = self._load(self._signals_file)
        return [s for s in signals if now - s["alert_time_ts"] < self._max_age]

    def get_history(self) -> List[dict]:
        with self._lock:
            return self._load(self._history_file)

    @property
    def max_age_hours(self) -> int:
        return int(self._max_age // 3600)

    # ── background loop ──────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        logger.info("Tracker background loop started (every %ds)", self._update_interval)
        while self._running:
            try:
                self.fetch_and_apply()
                self._check_take_profits()
                archived = self.archive_expired()
                if archived:
                    logger.info("Tracker: archived %d expired signals", archived)
            except Exception:
                logger.error("Tracker loop error", exc_info=True)
            self._sleep(self._update_interval)

    def stop(self) -> None:
        self._running = False

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(1.0)