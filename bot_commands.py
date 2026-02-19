"""
Telegram command listener.

Commands:
  /report          — all active signals with enriched details
  /report SYMBOL   — detailed single-coin breakdown
  /summary         — win rate, averages, best/worst
  /active          — quick list of tracked symbols
  /help            — command reference
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import requests

from binance_client import BinanceClient
from tracker import SignalTracker

logger = logging.getLogger(__name__)


class TelegramCommandListener:

    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        tracker: SignalTracker,
        binance: BinanceClient,
    ) -> None:
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._tracker = tracker
        self._binance = binance
        self._session = requests.Session()
        self._offset: int = 0
        self._running = False

    # ── telegram helpers ─────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return self.API.format(token=self._token, method=method)

    def _send(self, chat_id: str, text: str) -> bool:
        MAX_LEN = 4000
        parts: list[str] = []
        while len(text) > MAX_LEN:
            idx = text.rfind("\n", 0, MAX_LEN)
            if idx == -1:
                idx = MAX_LEN
            parts.append(text[:idx])
            text = text[idx:].lstrip("\n")
        parts.append(text)

        for part in parts:
            if not part.strip():
                continue
            for attempt in range(3):
                try:
                    r = self._session.post(
                        self._url("sendMessage"),
                        json={
                            "chat_id": chat_id,
                            "text": part,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                        timeout=15,
                    ).json()
                    if r.get("ok"):
                        break
                    if r.get("error_code") == 429:
                        wait = r.get("parameters", {}).get("retry_after", 30)
                        time.sleep(wait)
                        continue
                    logger.error("Telegram send error: %s", r)
                    return False
                except Exception as exc:
                    logger.error("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                    time.sleep(2)
            time.sleep(0.3)
        return True

    def _poll(self) -> list:
        try:
            resp = self._session.get(
                self._url("getUpdates"),
                params={
                    "offset": self._offset,
                    "timeout": 10,
                    "allowed_updates": '["message"]',
                },
                timeout=15,
            ).json()
            if not resp.get("ok"):
                return []
            return resp.get("result", [])
        except Exception:
            return []

    # ── formatting helpers ───────────────────────────────────────────

    @staticmethod
    def _fmt_price(price: float) -> str:
        if price <= 0:
            return "N/A"
        if price >= 1000:
            return f"${price:,.2f}"
        if price >= 1:
            return f"${price:.4f}"
        if price >= 0.001:
            return f"${price:.6f}"
        return f"${price:.8f}"

    @staticmethod
    def _fmt_pct(pct: float) -> str:
        icon = "📈" if pct >= 0 else "📉"
        return f"{icon} {pct:+.2f}%"

    @staticmethod
    def _fmt_age(ts: float) -> str:
        age = time.time() - ts
        if age < 3600:
            return f"{int(age / 60)}m ago"
        hours = int(age // 3600)
        mins = int((age % 3600) // 60)
        return f"{hours}h {mins}m ago"

    @staticmethod
    def _fmt_vol(vol: float) -> str:
        if vol >= 1e9:
            return f"${vol / 1e9:.1f}B"
        if vol >= 1e6:
            return f"${vol / 1e6:.1f}M"
        if vol >= 1e3:
            return f"${vol / 1e3:.0f}K"
        return f"${vol:.0f}"

    @staticmethod
    def _calc_pct(entry: float, current: float) -> float:
        if entry <= 0:
            return 0.0
        return ((current - entry) / entry) * 100.0

    @staticmethod
    def _pattern_emoji(pattern: str) -> str:
        return pattern.replace("G", "🟢").replace("R", "🔴")

    @staticmethod
    def _color_emoji(color: str) -> str:
        if color == "GREEN":
            return "🟢"
        if color == "RED":
            return "🔴"
        return "⚪"

    # ── main loop ────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        logger.info("Telegram command listener started")
        updates = self._poll()
        if updates:
            self._offset = updates[-1]["update_id"] + 1
            logger.info("Skipped %d old queued messages", len(updates))
        while self._running:
            try:
                updates = self._poll()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    self._handle(update)
            except Exception:
                logger.error("Command listener error", exc_info=True)
                time.sleep(5)

    def stop(self) -> None:
        self._running = False

    # ── dispatcher ───────────────────────────────────────────────────

    def _handle(self, update: dict) -> None:
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()

        if chat_id != self._chat_id:
            return

        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        logger.info("Command received: %s %s", cmd, args)

        handlers = {
            "/report":  lambda: self._cmd_report(chat_id, args),
            "/summary": lambda: self._cmd_summary(chat_id),
            "/active":  lambda: self._cmd_active(chat_id),
            "/help":    lambda: self._cmd_help(chat_id),
            "/start":   lambda: self._cmd_help(chat_id),
        }

        handler = handlers.get(cmd)
        if handler:
            handler()
        else:
            self._send(chat_id, "❓ Unknown command. Send /help for available commands.")

    # ── /report (all signals) ────────────────────────────────────────

    def _cmd_report(self, chat_id: str, args: list) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        if not signals:
            self._send(chat_id, "📊 No active signals in tracking window.")
            return

        # single symbol — detailed view
        if args:
            sym = args[0].upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            matches = [s for s in signals if s["symbol"] == sym]
            if not matches:
                self._send(chat_id, f"📊 No active signals found for <b>{sym}</b>")
                return
            for sig in matches:
                self._send_detailed_report(chat_id, sig, prices)
            return

        # all signals — condensed view
        signals.sort(key=lambda s: s["alert_time_ts"], reverse=True)

        lines = ["📊 <b>SIGNAL PERFORMANCE REPORT</b>", "━" * 28, ""]
        valid_changes: list[float] = []
        valid_highest: list[float] = []

        for sig in signals:
            sym = sig["symbol"]
            entry = sig.get("entry_price", 0)
            highest = sig.get("highest_price", entry)
            current = prices.get(sym, sig.get("current_price", 0))

            if current > highest:
                highest = current

            age = self._fmt_age(sig["alert_time_ts"])
            color = self._color_emoji(sig.get("candle_color", ""))
            body = sig.get("body_pct", 0)
            wick = sig.get("upper_wick_pct", 0)
            vol_r = sig.get("vol_ratio", 0)
            vol_fmt = sig.get("recent_vol_fmt", "N/A")
            trend_g = sig.get("trend_green", 0)
            trend_t = sig.get("trend_total", 0)
            brk_margin = sig.get("breakout_margin_pct")

            if entry > 0 and current > 0:
                cur_pct = self._calc_pct(entry, current)
                high_pct = self._calc_pct(entry, highest)
                valid_changes.append(cur_pct)
                valid_highest.append(high_pct)

                lines.append(f"📌 <b>{sym}</b>")
                lines.append(
                    f"   {self._fmt_price(entry)} → "
                    f"Now: {self._fmt_price(current)} ({self._fmt_pct(cur_pct)})"
                )
                lines.append(
                    f"   Peak: {self._fmt_price(highest)} ({self._fmt_pct(high_pct)})"
                )

                detail_parts = [f"{color} body:{body:.0f}% wick:{wick:.0f}%"]
                detail_parts.append(f"Vol: {vol_r:.1f}x ({vol_fmt})")
                if trend_t > 0:
                    detail_parts.append(f"Trend: {trend_g}/{trend_t}🟢")
                lines.append(f"   {' | '.join(detail_parts)}")

                extra = []
                if brk_margin is not None:
                    extra.append(f"Brk: +{brk_margin:.1f}%")
                oi = sig.get("oi_pct")
                if oi is not None:
                    extra.append(f"OI: +{oi:.1f}%")
                extra.append(f"Age: {age}")
                lines.append(f"   {' | '.join(extra)}")
                lines.append("")
            else:
                lines.append(f"📌 <b>{sym}</b> — entry price N/A — {age}")
                lines.append("")

        if valid_changes:
            total = len(valid_changes)
            avg_cur = sum(valid_changes) / total
            avg_high = sum(valid_highest) / total
            winners = sum(1 for c in valid_changes if c > 0)
            peak_winners = sum(1 for h in valid_highest if h > 2)

            lines.append("─" * 28)
            lines.append(f"📡 Active: {total} signals")
            lines.append(f"📊 Avg now:  {self._fmt_pct(avg_cur)}")
            lines.append(f"🏔  Avg peak: {self._fmt_pct(avg_high)}")
            lines.append(
                f"🎯 Win now:  {winners}/{total} ({winners / total * 100:.0f}%)"
            )
            lines.append(
                f"🎯 Win peak(>2%): {peak_winners}/{total} ({peak_winners / total * 100:.0f}%)"
            )

        self._send(chat_id, "\n".join(lines))

    # ── detailed single-coin report ──────────────────────────────────

    def _send_detailed_report(
        self, chat_id: str, sig: dict, prices: dict
    ) -> None:
        sym = sig["symbol"]
        entry = sig.get("entry_price", 0)
        highest = sig.get("highest_price", entry)
        current = prices.get(sym, sig.get("current_price", 0))

        if current > highest:
            highest = current

        cur_pct = self._calc_pct(entry, current) if entry > 0 else 0
        high_pct = self._calc_pct(entry, highest) if entry > 0 else 0
        age = self._fmt_age(sig["alert_time_ts"])

        lines = [
            f"📊 <b>DETAILED REPORT — {sym}</b>",
            "━" * 28,
            "",
            "<b>💵 Price Performance</b>",
            f"   Entry:    {self._fmt_price(entry)}",
            f"   Current:  {self._fmt_price(current)}  ({self._fmt_pct(cur_pct)})",
            f"   Highest:  {self._fmt_price(highest)}  ({self._fmt_pct(high_pct)})",
            f"   Age:      {age}",
            "",
            "<b>🕯 Candle Quality</b>",
            f"   Color:       {self._color_emoji(sig.get('candle_color', ''))} {sig.get('candle_color', 'N/A')}",
            f"   Body:        {sig.get('body_pct', 0):.1f}% of range",
            f"   Upper Wick:  {sig.get('upper_wick_pct', 0):.1f}% of range",
            f"   Lower Wick:  {sig.get('lower_wick_pct', 0):.1f}% of range",
            "",
            "<b>📊 Volume</b>",
            f"   Ratio:     {sig.get('vol_ratio', 0):.2f}x",
            f"   Recent:    {sig.get('recent_vol_fmt', 'N/A')} avg",
            f"   Baseline:  {sig.get('baseline_vol_fmt', 'N/A')} avg",
            "",
        ]

        # breakout section
        brk_level = sig.get("breakout_level")
        brk_margin = sig.get("breakout_margin_pct")
        if sig.get("breakout_confirmed"):
            lines.append("<b>🔺 Breakout</b>")
            lines.append(
                f"   Level:   {self._fmt_price(brk_level)}"
                if brk_level else "   Level:   N/A"
            )
            lines.append(
                f"   Margin:  +{brk_margin:.2f}% above level"
                if brk_margin is not None else "   Margin:  N/A"
            )
            lines.append("")

        # OI section
        oi = sig.get("oi_pct")
        if oi is not None:
            lines.append("<b>📈 Open Interest</b>")
            lines.append(f"   Change:  {self._fmt_pct(oi)}")
            lines.append("")

        # trend section
        pattern = sig.get("trend_pattern", "")
        trend_g = sig.get("trend_green", 0)
        trend_t = sig.get("trend_total", 0)
        if pattern:
            lines.append("<b>📊 Trend Context</b>")
            lines.append(
                f"   Last {trend_t} candles: {trend_g}/{trend_t} green  "
                f"{self._pattern_emoji(pattern)}"
            )

        # BTC context
        btc_at_signal = sig.get("btc_price")
        btc_now = prices.get("BTCUSDT")
        if btc_at_signal and btc_now:
            btc_chg = self._calc_pct(btc_at_signal, btc_now)
            lines.append(
                f"   BTC:  {self._fmt_price(btc_at_signal)} → "
                f"{self._fmt_price(btc_now)} ({self._fmt_pct(btc_chg)})"
            )

        lines.append("")
        lines.append(f"💰 Market Cap: {sig.get('mcap', 'Unknown')}")
        lines.append(f"🕐 Signal: {sig.get('alert_time', 'N/A')}")

        # diagnosis hint
        lines.append("")
        lines.append(self._diagnosis(sig, cur_pct, high_pct))

        self._send(chat_id, "\n".join(lines))

    # ── auto diagnosis ───────────────────────────────────────────────

    @staticmethod
    def _diagnosis(sig: dict, cur_pct: float, high_pct: float) -> str:
        """Generate a brief diagnostic hint based on signal data."""
        hints: list[str] = []

        wick = sig.get("upper_wick_pct", 0)
        body = sig.get("body_pct", 0)
        brk_margin = sig.get("breakout_margin_pct")
        trend_g = sig.get("trend_green", 0)
        trend_t = sig.get("trend_total", 5)
        vol_ratio = sig.get("vol_ratio", 0)

        if high_pct > 5 and cur_pct < 0:
            hints.append("⚠️ Pumped then dumped — possible distribution")
        elif high_pct < 1 and cur_pct < -3:
            hints.append("⚠️ Never pumped — signal may have been late entry")

        if wick > 40:
            hints.append(f"⚠️ High upper wick ({wick:.0f}%) — selling pressure at signal")

        if body < 35:
            hints.append(f"⚠️ Small body ({body:.0f}%) — weak conviction candle")

        if brk_margin is not None and brk_margin < 0.5:
            hints.append(f"⚠️ Marginal breakout (+{brk_margin:.1f}%) — barely broke out")
        elif brk_margin is not None and brk_margin > 3:
            hints.append(f"✅ Strong breakout (+{brk_margin:.1f}%)")

        if trend_t > 0 and trend_g / trend_t < 0.4:
            hints.append(f"⚠️ Weak trend ({trend_g}/{trend_t} green) — counter-trend signal")
        elif trend_t > 0 and trend_g / trend_t >= 0.8:
            hints.append(f"✅ Strong trend ({trend_g}/{trend_t} green)")

        if vol_ratio > 10:
            hints.append(f"✅ Extreme volume ({vol_ratio:.0f}x) — high conviction")
        elif vol_ratio < 3.5:
            hints.append(f"⚠️ Volume barely above threshold ({vol_ratio:.1f}x)")

        if cur_pct > 5:
            hints.append(f"✅ Currently profitable ({cur_pct:+.1f}%)")

        if not hints:
            hints.append("ℹ️ Signal looks normal — no strong flags")

        return "<b>🔍 Diagnosis</b>\n" + "\n".join(f"   {h}" for h in hints)

    # ── /summary ─────────────────────────────────────────────────────

    def _cmd_summary(self, chat_id: str) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        history = self._tracker.get_history()

        lines = ["📊 <b>PERFORMANCE SUMMARY</b>", "━" * 28, ""]

        # active signals
        active_valid = [s for s in signals if s.get("entry_price", 0) > 0]
        if active_valid:
            changes: list[float] = []
            highest_changes: list[float] = []
            for s in active_valid:
                cur = prices.get(s["symbol"], s.get("current_price", s["entry_price"]))
                changes.append(self._calc_pct(s["entry_price"], cur))
                highest_changes.append(
                    self._calc_pct(
                        s["entry_price"],
                        max(s.get("highest_price", s["entry_price"]), cur),
                    )
                )

            winners = sum(1 for c in changes if c > 0)
            peak_w = sum(1 for h in highest_changes if h > 2)
            best_i = changes.index(max(changes))
            worst_i = changes.index(min(changes))
            best_h_i = highest_changes.index(max(highest_changes))

            # quality breakdown
            strong_body = sum(
                1 for s in active_valid if s.get("body_pct", 0) >= 50
            )
            strong_trend = sum(
                1 for s in active_valid
                if s.get("trend_total", 0) > 0
                and s.get("trend_green", 0) / s["trend_total"] >= 0.6
            )

            lines.append(f"<b>📡 Active Signals ({len(active_valid)})</b>")
            lines.append(f"   Avg Current:  {self._fmt_pct(sum(changes) / len(changes))}")
            lines.append(
                f"   Avg Peak:     "
                f"{self._fmt_pct(sum(highest_changes) / len(highest_changes))}"
            )
            lines.append(
                f"   Win Now:      {winners}/{len(active_valid)}"
                f" ({winners / len(active_valid) * 100:.0f}%)"
            )
            lines.append(
                f"   Win Peak>2%:  {peak_w}/{len(active_valid)}"
                f" ({peak_w / len(active_valid) * 100:.0f}%)"
            )
            lines.append(
                f"   Best Now:     {active_valid[best_i]['symbol']}"
                f"  {self._fmt_pct(changes[best_i])}"
            )
            lines.append(
                f"   Worst Now:    {active_valid[worst_i]['symbol']}"
                f"  {self._fmt_pct(changes[worst_i])}"
            )
            lines.append(
                f"   Best Peak:    {active_valid[best_h_i]['symbol']}"
                f"  {self._fmt_pct(highest_changes[best_h_i])}"
            )
            lines.append("")
            lines.append("<b>📋 Signal Quality Breakdown</b>")
            lines.append(f"   Strong body (≥50%):   {strong_body}/{len(active_valid)}")
            lines.append(f"   Strong trend (≥60%):  {strong_trend}/{len(active_valid)}")
            lines.append("")
        else:
            lines.append("📡 No active signals")
            lines.append("")

        # history
        if history:
            exit_pcts = [h["exit_pct"] for h in history if h.get("exit_pct") is not None]
            high_pcts = [h["highest_pct"] for h in history if h.get("highest_pct") is not None]
            if exit_pcts:
                h_winners = sum(1 for p in exit_pcts if p > 0)
                lines.append(f"<b>📜 History ({len(history)} signals)</b>")
                lines.append(
                    f"   Avg Exit:  {self._fmt_pct(sum(exit_pcts) / len(exit_pcts))}"
                )
                if high_pcts:
                    lines.append(
                        f"   Avg Peak:  {self._fmt_pct(sum(high_pcts) / len(high_pcts))}"
                    )
                lines.append(
                    f"   Win Rate:  {h_winners}/{len(exit_pcts)}"
                    f" ({h_winners / len(exit_pcts) * 100:.0f}%)"
                )
            else:
                lines.append(f"📜 History: {len(history)} signals (no exit data)")
        else:
            lines.append("📜 No historical signals yet")

        self._send(chat_id, "\n".join(lines))

    # ── /active ──────────────────────────────────────────────────────

    def _cmd_active(self, chat_id: str) -> None:
        signals = self._tracker.get_active_signals()
        if not signals:
            self._send(chat_id, "📡 No active signals being tracked.")
            return

        signals.sort(key=lambda s: s["alert_time_ts"], reverse=True)

        lines = [
            f"📡 <b>ACTIVE SIGNALS ({len(signals)})</b>",
            "━" * 28,
            "",
        ]
        for sig in signals:
            age = self._fmt_age(sig["alert_time_ts"])
            color = self._color_emoji(sig.get("candle_color", ""))
            vol = sig.get("vol_ratio", 0)
            lines.append(
                f"• <b>{sig['symbol']}</b>  {color}  {age}  "
                f"{vol:.1f}x  {sig.get('mcap', '?')}"
            )

        lines.append("")
        lines.append(f"Window: {self._tracker.max_age_hours}h | Send /report SYMBOL for details")
        self._send(chat_id, "\n".join(lines))

    # ── /help ────────────────────────────────────────────────────────

    def _cmd_help(self, chat_id: str) -> None:
        text = (
            "🤖 <b>VOLUME SCANNER COMMANDS</b>\n"
            + "━" * 28
            + "\n\n"
            "/report — All signals with performance + context\n"
            "/report SYMBOL — Detailed breakdown + auto diagnosis\n"
            "/summary — Win rates, averages, quality stats\n"
            "/active — Quick list of tracked signals\n"
            "/help — Show this message\n\n"
            f"💡 Signals tracked for {self._tracker.max_age_hours}h\n"
            "🏔 Highest price updated every 5 min\n"
            "🔍 Single-coin report includes auto diagnosis"
        )
        self._send(chat_id, text)