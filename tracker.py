"""
Signal performance tracker with take-profit alerts.

Responsibilities:
  - Store every alert to disk with full enrichment (including additional_data)
  - Continuously track highest price AND lowest price since entry
  - Send take-profit target alerts when price hits configurable levels
  - Send reversal warnings when price drops significantly from peak
  - Archive signals after configurable max age
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from binance_client import BinanceClient
from market_cap import MarketCapProvider
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class SignalTracker:

    def __init__(
        self,
        config: dict,
        binance: BinanceClient,
        notifier: TelegramNotifier,
        market_cap: Optional[MarketCapProvider] = None,
    ) -> None:
        tc = config.get("tracker", {})
        self._max_age = tc.get("max_age_hours", 168) * 3600
        self._update_interval = tc.get("price_update_interval_seconds", 300)
        self._data_dir = Path(tc.get("data_dir", "data"))
        self._signals_file = self._data_dir / "signals.json"
        self._history_file = self._data_dir / "history.json"

        self._tp_targets: List[int] = sorted(tc.get("take_profit_targets", [5, 10, 15, 20]))
        self._reversal_enabled: bool = tc.get("reversal_alert_enabled", True)
        self._min_reversal_peak: float = tc.get("min_reversal_peak_pct", 3.0)
        self._reversal_drop: float = tc.get("reversal_drop_from_peak_pct", 5.0)
        self._detailed_min_age: float = tc.get("detailed_report_min_age_hours", 168) * 3600
        self._daily_report_hour: int = int(tc.get("daily_report_hour", 0))

        self._pending_file = self._data_dir / "pending_report.json"
        self._last_report_file = self._data_dir / "last_report_date.txt"

        self._binance = binance
        self._notifier = notifier
        self._market_cap = market_cap
        self._lock = threading.Lock()
        self._running = False

        self._data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Tracker initialised  (max_age=%dh, update=%ds, TP targets=%s, reversal=%s, report_hour=%02d:00 UTC)",
            self._max_age // 3600, self._update_interval,
            self._tp_targets, self._reversal_enabled, self._daily_report_hour,
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
            "lowest_price":        price,
            "current_price":       price,
            "alert_time_ts":       time.time(),
            "alert_time":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "timeframe":           alert.get("timeframe", "1h"),
            "price_change_24h":    alert.get("price_change_24h", 0),
            "breakout_margin_pct": alert.get("breakout_margin_pct"),
            "high_24h":            alert.get("high_24h"),
            "vol_candle_1":        alert.get("vol_candle_1"),
            "vol_candle_2":        alert.get("vol_candle_2"),
            "vol_candle_3":        alert.get("vol_candle_3"),
            "vol_candle_1_fmt":    alert.get("vol_candle_1_fmt"),
            "vol_candle_2_fmt":    alert.get("vol_candle_2_fmt"),
            "vol_candle_3_fmt":    alert.get("vol_candle_3_fmt"),
            "rvol":                alert.get("rvol"),
            "btc_price":           alert.get("btc_price"),
            "candle_time":         alert.get("candle_time"),
            "additional_data":     alert.get("additional_data", {}),
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
                lowest = sig.get("lowest_price", current)
                if lowest == 0 or current < lowest:
                    sig["lowest_price"] = current
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

                tp_sent: list = sig.get("tp_sent", [])

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
            newly_archived = []

            for sig in signals:
                age = now - sig["alert_time_ts"]
                if age >= self._max_age:
                    entry = sig.get("entry_price", 0)
                    highest = sig.get("highest_price", 0)
                    lowest = sig.get("lowest_price", 0)
                    current = sig.get("current_price", 0)
                    sig["archived_time_ts"] = now
                    sig["archived_time"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    sig["tracked_hours"] = round(age / 3600, 1)
                    if entry > 0:
                        sig["peak_pct"] = round(((highest - entry) / entry) * 100, 2)
                        sig["lowest_pct"] = round(((lowest - entry) / entry) * 100, 2) if lowest > 0 else None
                        sig["exit_pct"] = round(((current - entry) / entry) * 100, 2)
                        sig["exit_price"] = current
                        sig["highest_pct"] = sig["peak_pct"]
                    if self._market_cap is not None:
                        try:
                            base = sig["symbol"].replace("USDT", "").replace("BUSD", "")
                            sig["market_cap_usd_exit"] = self._market_cap.get(base)
                            sig["market_cap_exit_fmt"] = self._market_cap.format(base)
                        except Exception:
                            pass
                    history.append(sig)
                    newly_archived.append(sig)
                    archived += 1
                else:
                    active.append(sig)

            if archived > 0:
                self._save(self._signals_file, active)
                self._save(self._history_file, history)

        if archived > 0:
            self._add_to_pending(newly_archived)

        return archived

    def _add_to_pending(self, signals: list) -> None:
        with self._lock:
            pending = self._load(self._pending_file)
            pending.extend(signals)
            self._save(self._pending_file, pending)
        logger.info("Queued %d signal(s) for daily report", len(signals))

    def _check_daily_report(self) -> None:
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour != self._daily_report_hour:
            return

        today_str = now_utc.strftime("%Y-%m-%d")

        last_sent = ""
        if self._last_report_file.exists():
            try:
                last_sent = self._last_report_file.read_text(encoding="utf-8").strip()
            except IOError:
                pass

        if last_sent == today_str:
            return

        with self._lock:
            pending = self._load(self._pending_file)
            if not pending:
                self._last_report_file.write_text(today_str, encoding="utf-8")
                return

        tmp_path = self._data_dir / f"report_{today_str}.json"
        sent = False
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(pending, fh, indent=2)
            count = len(pending)
            symbols = ", ".join(s["symbol"] for s in pending)
            caption = (
                f"Daily 7-day report — {count} signal{'s' if count != 1 else ''} completed\n"
                f"{symbols}"
            )
            sent = self._notifier.send_document(str(tmp_path), caption=caption)
            if sent:
                logger.info("Sent daily report for %d signal(s): %s", count, symbols)
            else:
                logger.error("Failed to send daily report — will retry next cycle")
        except Exception as exc:
            logger.error("Failed to send daily report: %s", exc)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        if sent:
            with self._lock:
                self._save(self._pending_file, [])
            try:
                self._last_report_file.write_text(today_str, encoding="utf-8")
            except IOError as exc:
                logger.error("Failed to save last_report_date: %s", exc)

    # ── data access ──────────────────────────────────────────────────

    def get_active_signals(self) -> List[dict]:
        now = time.time()
        with self._lock:
            signals = self._load(self._signals_file)
        return [s for s in signals if now - s["alert_time_ts"] < self._max_age]

    def get_tracked_symbols(self) -> Set[str]:
        with self._lock:
            signals = self._load(self._signals_file)
        return {s["symbol"] for s in signals}

    def get_history(self) -> List[dict]:
        with self._lock:
            return self._load(self._history_file)

    def get_completed_signals(self, min_age_seconds: float) -> List[dict]:
        """Return archived signals that have been tracked for at least min_age_seconds."""
        now = time.time()
        history = self.get_history()
        return [
            h for h in history
            if (now - h.get("alert_time_ts", now)) >= min_age_seconds
        ]

    @property
    def max_age_hours(self) -> int:
        return int(self._max_age // 3600)

    @property
    def detailed_report_min_age_seconds(self) -> float:
        return self._detailed_min_age

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
                self._check_daily_report()
            except Exception:
                logger.error("Tracker loop error", exc_info=True)
            self._sleep(self._update_interval)

    def stop(self) -> None:
        self._running = False

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(1.0)
