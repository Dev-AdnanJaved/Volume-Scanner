"""
Telegram command listener.

Polls for incoming messages from the authorised chat and responds
to /report, /summary, /active, /help commands with live signal
performance data.
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

    # ── Telegram helpers ─────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return self.API.format(token=self._token, method=method)

    def _send(self, chat_id: str, text: str) -> bool:
        """Send a message, auto-splitting if it exceeds Telegram's limit."""
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
                        logger.warning("Telegram 429 in commands — waiting %ds", wait)
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
    def _calc_pct(entry: float, current: float) -> float:
        if entry <= 0:
            return 0.0
        return ((current - entry) / entry) * 100.0

    # ── main loop ────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        logger.info("Telegram command listener started")

        # skip messages that arrived while bot was offline
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
            logger.warning("Ignored command from unauthorised chat %s", chat_id)
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

    # ── /report ──────────────────────────────────────────────────────

    def _cmd_report(self, chat_id: str, args: list) -> None:
        # fetch latest prices and update tracker
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()

        if not signals:
            self._send(chat_id, "📊 No active signals in tracking window.")
            return

        # filter by symbol if specified
        if args:
            sym = args[0].upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            signals = [s for s in signals if s["symbol"] == sym]
            if not signals:
                self._send(chat_id, f"📊 No active signals found for <b>{sym}</b>")
                return

        # sort newest first
        signals.sort(key=lambda s: s["alert_time_ts"], reverse=True)

        lines = ["📊 <b>SIGNAL PERFORMANCE REPORT</b>", "━" * 28, ""]

        valid_changes: list[float] = []
        valid_highest: list[float] = []

        for sig in signals:
            sym = sig["symbol"]
            entry = sig.get("entry_price", 0)
            highest = sig.get("highest_price", entry)
            current = prices.get(sym, sig.get("current_price", 0))

            # update highest on the fly
            if current > highest:
                highest = current

            age = self._fmt_age(sig["alert_time_ts"])

            if entry > 0 and current > 0:
                cur_pct = self._calc_pct(entry, current)
                high_pct = self._calc_pct(entry, highest)
                valid_changes.append(cur_pct)
                valid_highest.append(high_pct)

                lines.append(f"📌 <b>{sym}</b>")
                lines.append(
                    f"   Entry:    {self._fmt_price(entry)}"
                )
                lines.append(
                    f"   Current:  {self._fmt_price(current)}  ({self._fmt_pct(cur_pct)})"
                )
                lines.append(
                    f"   Highest:  {self._fmt_price(highest)}  ({self._fmt_pct(high_pct)})"
                )
                lines.append(f"   Age:      {age}")
                lines.append(
                    f"   Vol:      {sig.get('vol_ratio', 0):.2f}x  |  MCap: {sig.get('mcap', 'N/A')}"
                )
                lines.append("")
            else:
                lines.append(f"📌 <b>{sym}</b>")
                lines.append("   Entry price unavailable")
                lines.append(f"   Age: {age}")
                lines.append("")

        # footer summary
        if valid_changes:
            total = len(valid_changes)
            avg_cur = sum(valid_changes) / total
            avg_high = sum(valid_highest) / total
            winners = sum(1 for c in valid_changes if c > 0)

            lines.append("─" * 28)
            lines.append(f"📡 Active signals: {total}")
            lines.append(f"📊 Avg current:  {self._fmt_pct(avg_cur)}")
            lines.append(f"🏔  Avg highest:  {self._fmt_pct(avg_high)}")
            lines.append(
                f"🎯 Win rate:     {winners}/{total} ({winners / total * 100:.0f}%)"
            )

        self._send(chat_id, "\n".join(lines))

    # ── /summary ─────────────────────────────────────────────────────

    def _cmd_summary(self, chat_id: str) -> None:
        # fetch latest prices
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        history = self._tracker.get_history()

        lines = ["📊 <b>PERFORMANCE SUMMARY</b>", "━" * 28, ""]

        # ── active signals ───────────────────────────────────────────
        active_valid = [
            s for s in signals
            if s.get("entry_price", 0) > 0
        ]
        if active_valid:
            changes: list[float] = []
            highest_changes: list[float] = []
            for s in active_valid:
                cur = prices.get(s["symbol"], s.get("current_price", s["entry_price"]))
                changes.append(self._calc_pct(s["entry_price"], cur))
                highest_changes.append(
                    self._calc_pct(s["entry_price"], s.get("highest_price", s["entry_price"]))
                )

            winners = sum(1 for c in changes if c > 0)
            best_i = changes.index(max(changes))
            worst_i = changes.index(min(changes))
            best_h_i = highest_changes.index(max(highest_changes))

            lines.append(f"<b>📡 Active Signals ({len(active_valid)})</b>")
            lines.append(f"   Avg Current:  {self._fmt_pct(sum(changes) / len(changes))}")
            lines.append(
                f"   Avg Highest:  {self._fmt_pct(sum(highest_changes) / len(highest_changes))}"
            )
            lines.append(
                f"   Win Rate:     {winners}/{len(active_valid)}"
                f" ({winners / len(active_valid) * 100:.0f}%)"
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
        else:
            lines.append("📡 No active signals")
            lines.append("")

        # ── history ──────────────────────────────────────────────────
        if history:
            exit_pcts = [
                h["exit_pct"] for h in history
                if h.get("exit_pct") is not None
            ]
            high_pcts = [
                h["highest_pct"] for h in history
                if h.get("highest_pct") is not None
            ]
            if exit_pcts:
                h_winners = sum(1 for p in exit_pcts if p > 0)
                lines.append(f"<b>📜 History ({len(history)} signals)</b>")
                lines.append(
                    f"   Avg Exit:     {self._fmt_pct(sum(exit_pcts) / len(exit_pcts))}"
                )
                if high_pcts:
                    lines.append(
                        f"   Avg Peak:     {self._fmt_pct(sum(high_pcts) / len(high_pcts))}"
                    )
                lines.append(
                    f"   Win Rate:     {h_winners}/{len(exit_pcts)}"
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
            lines.append(
                f"• <b>{sig['symbol']}</b>  —  {age}  —  "
                f"{sig.get('vol_ratio', 0):.1f}x vol  —  {sig.get('mcap', '?')}"
            )

        lines.append("")
        lines.append(f"Tracking window: {self._tracker.max_age_hours}h")
        self._send(chat_id, "\n".join(lines))

    # ── /help ────────────────────────────────────────────────────────

    def _cmd_help(self, chat_id: str) -> None:
        text = (
            "🤖 <b>VOLUME SCANNER COMMANDS</b>\n"
            + "━" * 28
            + "\n\n"
            "/report — Performance report of all active signals\n"
            "/report SYMBOL — Report for one coin (e.g. /report ARC)\n"
            "/summary — Overall statistics (active + history)\n"
            "/active — List all currently tracked signals\n"
            "/help — Show this message\n\n"
            f"💡 Signals tracked for up to {self._tracker.max_age_hours}h.\n"
            "🏔 Highest price updated every 5 min.\n"
            "📊 /report fetches live prices on demand."
        )
        self._send(chat_id, text)