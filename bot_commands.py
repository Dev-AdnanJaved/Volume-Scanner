"""
Telegram command listener.

Commands:
  /report          — all active signals with performance
  /report SYMBOL   — detailed single-coin breakdown with diagnosis
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
        icon = "🟢" if pct > 0 else "🔴" if pct < 0 else "⚪"
        return f"{icon} {pct:+.2f}%"

    @staticmethod
    def _fmt_age(ts: float) -> str:
        age = time.time() - ts
        if age < 3600:
            return f"{int(age / 60)}m"
        hours = int(age // 3600)
        mins = int((age % 3600) // 60)
        return f"{hours}h {mins}m"

    @staticmethod
    def _calc_pct(entry: float, current: float) -> float:
        if entry <= 0:
            return 0.0
        return ((current - entry) / entry) * 100.0

    @staticmethod
    def _result_emoji(pct: float) -> str:
        if pct >= 10:
            return "🚀"
        if pct >= 5:
            return "✅"
        if pct >= 0:
            return "🟢"
        if pct >= -5:
            return "🟡"
        return "🔴"

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
            "/report":   lambda: self._cmd_report(chat_id, args),
            "/analysis": lambda: self._cmd_analysis(chat_id, args),    # ← ADD
            "/summary":  lambda: self._cmd_summary(chat_id),
            "/active":   lambda: self._cmd_active(chat_id),
            "/help":     lambda: self._cmd_help(chat_id),
            "/start":    lambda: self._cmd_help(chat_id),
        }
        handler = handlers.get(cmd)
        if handler:
            handler()
        else:
            self._send(chat_id, "❓ Unknown command. Send /help")

    # ── /report (all signals) ────────────────────────────────────────

    def _cmd_report(self, chat_id: str, args: list) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        if not signals:
            self._send(chat_id, "📊 No active signals.")
            return

        # single symbol → detailed view
        if args:
            sym = args[0].upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            matches = [s for s in signals if s["symbol"] == sym]
            if not matches:
                self._send(chat_id, f"📊 No active signal for <b>{sym}</b>")
                return
            for sig in matches:
                self._send_detailed_report(chat_id, sig, prices)
            return

        # all signals → clean compact view
        signals.sort(key=lambda s: s["alert_time_ts"], reverse=True)

        lines = ["📊 <b>PERFORMANCE REPORT</b>", ""]

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
            vol_r = sig.get("vol_ratio", 0)

            if entry > 0 and current > 0:
                cur_pct = self._calc_pct(entry, current)
                high_pct = self._calc_pct(entry, highest)
                valid_changes.append(cur_pct)
                valid_highest.append(high_pct)

                emoji = self._result_emoji(cur_pct)

                lines.append(
                    f"{emoji} <b>{sym}</b>  •  {age}"
                )
                lines.append(
                    f"   Now: {cur_pct:+.2f}%  │  Peak: {high_pct:+.2f}%  │  Vol: {vol_r:.1f}x"
                )
                lines.append("")
            else:
                lines.append(f"⚪ <b>{sym}</b>  •  {age}  •  No price data")
                lines.append("")

        # footer
        if valid_changes:
            total = len(valid_changes)
            avg_cur = sum(valid_changes) / total
            avg_high = sum(valid_highest) / total
            winners = sum(1 for c in valid_changes if c > 0)
            peak_w = sum(1 for h in valid_highest if h > 2)

            lines.append("━" * 26)
            lines.append(f"📡 Signals:    {total}")
            lines.append(f"📊 Avg now:    {avg_cur:+.2f}%")
            lines.append(f"🏔  Avg peak:   {avg_high:+.2f}%")
            lines.append(f"🎯 Win now:    {winners}/{total} ({winners/total*100:.0f}%)")
            lines.append(f"🎯 Win peak:   {peak_w}/{total} ({peak_w/total*100:.0f}%)")
            lines.append("")
            lines.append("💡 /report SYMBOL for details")

        self._send(chat_id, "\n".join(lines))

    # ── detailed single-coin report ──────────────────────────────────

    def _send_detailed_report(self, chat_id: str, sig: dict, prices: dict) -> None:
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
            f"📊 <b>{sym} — DETAILED</b>",
            "",
            "━━━ 💵 PRICE ━━━",
            f"Entry:     {self._fmt_price(entry)}",
            f"Current:   {self._fmt_price(current)}   {self._fmt_pct(cur_pct)}",
            f"Highest:   {self._fmt_price(highest)}   {self._fmt_pct(high_pct)}",
            f"Age:       {age}",
        ]

        # candle quality (only if data exists)
        candle_color = sig.get("candle_color", "")
        body = sig.get("body_pct", 0)
        wick = sig.get("upper_wick_pct", 0)
        has_candle_data = candle_color and (body > 0 or wick > 0)

        if has_candle_data:
            lines.append("")
            lines.append("━━━ 🕯 CANDLE QUALITY ━━━")
            lines.append(f"Color:       {self._color_emoji(candle_color)} {candle_color}")
            lines.append(f"Body size:   {body:.0f}%")
            lines.append(f"Upper wick:  {wick:.0f}%")
            lwick = sig.get("lower_wick_pct", 0)
            if lwick > 0:
                lines.append(f"Lower wick:  {lwick:.0f}%")

        # volume
        lines.append("")
        lines.append("━━━ 📊 VOLUME ━━━")
        lines.append(f"Spike:       {sig.get('vol_ratio', 0):.2f}x")
        recent_fmt = sig.get("recent_vol_fmt")
        baseline_fmt = sig.get("baseline_vol_fmt")
        if recent_fmt and recent_fmt != "N/A":
            lines.append(f"Recent avg:  {recent_fmt}")
            lines.append(f"Normal avg:  {baseline_fmt}")

        # breakout
        brk_margin = sig.get("breakout_margin_pct")
        brk_level = sig.get("breakout_level")
        if sig.get("breakout_confirmed"):
            lines.append("")
            lines.append("━━━ 🔺 BREAKOUT ━━━")
            if brk_level:
                lines.append(f"Level:       {self._fmt_price(brk_level)}")
            if brk_margin is not None:
                lines.append(f"Margin:      +{brk_margin:.2f}%")

        # open interest
        oi = sig.get("oi_pct")
        if oi is not None:
            lines.append("")
            lines.append("━━━ 📈 OPEN INTEREST ━━━")
            lines.append(f"Change:      {oi:+.2f}%")

        # trend
        pattern = sig.get("trend_pattern", "")
        trend_g = sig.get("trend_green", 0)
        trend_t = sig.get("trend_total", 0)
        if pattern:
            lines.append("")
            lines.append("━━━ 📊 TREND ━━━")
            lines.append(f"Pattern:     {self._pattern_emoji(pattern)}")
            lines.append(f"Green:       {trend_g}/{trend_t}")

        # BTC context
        btc_at = sig.get("btc_price")
        btc_now = prices.get("BTCUSDT")
        if btc_at and btc_now:
            btc_chg = self._calc_pct(btc_at, btc_now)
            lines.append("")
            lines.append("━━━ ₿ MARKET ━━━")
            lines.append(
                f"BTC:  {self._fmt_price(btc_at)} → {self._fmt_price(btc_now)}"
                f"  ({btc_chg:+.2f}%)"
            )

        lines.append("")
        lines.append(f"💰 MCap: {sig.get('mcap', 'Unknown')}")
        lines.append(f"🕐 Signal: {sig.get('alert_time', 'N/A')}")

        # diagnosis
        lines.append("")
        lines.append(self._diagnosis(sig, cur_pct, high_pct))

        self._send(chat_id, "\n".join(lines))

    # ── auto diagnosis ───────────────────────────────────────────────

    @staticmethod
    def _diagnosis(sig: dict, cur_pct: float, high_pct: float) -> str:
        hints: list[str] = []

        wick = sig.get("upper_wick_pct", 0)
        body = sig.get("body_pct", 0)
        brk_margin = sig.get("breakout_margin_pct")
        trend_g = sig.get("trend_green", 0)
        trend_t = sig.get("trend_total", 5)
        vol_ratio = sig.get("vol_ratio", 0)
        candle_color = sig.get("candle_color", "")

        # performance based
        if high_pct > 5 and cur_pct < 0:
            hints.append("⚠️ Pumped then dumped — distribution likely")
        elif high_pct < 1 and cur_pct < -3:
            hints.append("⚠️ Never pumped — weak entry / late signal")
        elif cur_pct > 10:
            hints.append("🚀 Strong winner — momentum confirmed")
        elif cur_pct > 5:
            hints.append("✅ Profitable — signal working well")

        # candle quality based (only if data exists)
        if candle_color == "RED":
            hints.append("⚠️ RED candle signal — selling volume")

        if wick > 40 and wick > 0:
            hints.append(f"⚠️ Upper wick {wick:.0f}% — sellers rejected the high")

        if 0 < body < 35:
            hints.append(f"⚠️ Body only {body:.0f}% — weak conviction")
        elif body >= 60:
            hints.append(f"✅ Strong body {body:.0f}% — decisive move")

        # breakout quality
        if brk_margin is not None:
            if brk_margin < 0.5:
                hints.append(f"⚠️ Barely broke out (+{brk_margin:.1f}%) — risky")
            elif brk_margin > 3:
                hints.append(f"✅ Clean breakout (+{brk_margin:.1f}%)")

        # trend based
        if trend_t > 0:
            ratio = trend_g / trend_t
            if ratio < 0.4:
                hints.append(f"⚠️ Weak trend ({trend_g}/{trend_t} green)")
            elif ratio >= 0.8:
                hints.append(f"✅ Strong trend ({trend_g}/{trend_t} green)")

        # volume
        if vol_ratio > 10:
            hints.append(f"✅ Extreme volume ({vol_ratio:.0f}x)")
        elif vol_ratio < 3.5:
            hints.append(f"⚠️ Volume barely met threshold ({vol_ratio:.1f}x)")

        if not hints:
            hints.append("ℹ️ No strong flags — normal signal")

        return "━━━ 🔍 DIAGNOSIS ━━━\n" + "\n".join(hints)

    # ── /summary ─────────────────────────────────────────────────────

    def _cmd_summary(self, chat_id: str) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        history = self._tracker.get_history()

        lines = ["📊 <b>SUMMARY</b>", ""]

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

            lines.append(f"━━━ 📡 ACTIVE ({len(active_valid)}) ━━━")
            lines.append(f"Avg now:    {sum(changes)/len(changes):+.2f}%")
            lines.append(f"Avg peak:   {sum(highest_changes)/len(highest_changes):+.2f}%")
            lines.append(f"Win now:    {winners}/{len(active_valid)} ({winners/len(active_valid)*100:.0f}%)")
            lines.append(f"Win peak:   {peak_w}/{len(active_valid)} ({peak_w/len(active_valid)*100:.0f}%)")
            lines.append("")
            lines.append(f"🚀 Best:     {active_valid[best_i]['symbol']} {changes[best_i]:+.2f}%")
            lines.append(f"🔴 Worst:    {active_valid[worst_i]['symbol']} {changes[worst_i]:+.2f}%")
            lines.append(f"🏔  Top peak:  {active_valid[best_h_i]['symbol']} {highest_changes[best_h_i]:+.2f}%")

            # quality breakdown
            has_quality = [s for s in active_valid if s.get("body_pct", 0) > 0]
            if has_quality:
                strong_body = sum(1 for s in has_quality if s.get("body_pct", 0) >= 50)
                strong_trend = sum(
                    1 for s in has_quality
                    if s.get("trend_total", 0) > 0
                    and s.get("trend_green", 0) / s["trend_total"] >= 0.6
                )
                lines.append("")
                lines.append(f"━━━ 📋 QUALITY ━━━")
                lines.append(f"Strong body:   {strong_body}/{len(has_quality)}")
                lines.append(f"Strong trend:  {strong_trend}/{len(has_quality)}")
        else:
            lines.append("📡 No active signals")

        lines.append("")

        if history:
            exit_pcts = [h["exit_pct"] for h in history if h.get("exit_pct") is not None]
            high_pcts = [h["highest_pct"] for h in history if h.get("highest_pct") is not None]
            if exit_pcts:
                h_win = sum(1 for p in exit_pcts if p > 0)
                lines.append(f"━━━ 📜 HISTORY ({len(history)}) ━━━")
                lines.append(f"Avg exit:   {sum(exit_pcts)/len(exit_pcts):+.2f}%")
                if high_pcts:
                    lines.append(f"Avg peak:   {sum(high_pcts)/len(high_pcts):+.2f}%")
                lines.append(f"Win rate:   {h_win}/{len(exit_pcts)} ({h_win/len(exit_pcts)*100:.0f}%)")
        else:
            lines.append("📜 No history yet")

        self._send(chat_id, "\n".join(lines))

    # ── /active ──────────────────────────────────────────────────────

    def _cmd_active(self, chat_id: str) -> None:
        signals = self._tracker.get_active_signals()
        if not signals:
            self._send(chat_id, "📡 No active signals.")
            return

        signals.sort(key=lambda s: s["alert_time_ts"], reverse=True)

        lines = [f"📡 <b>ACTIVE ({len(signals)})</b>", ""]

        for sig in signals:
            age = self._fmt_age(sig["alert_time_ts"])
            sym = sig["symbol"]
            vol = sig.get("vol_ratio", 0)
            mcap = sig.get("mcap", "?")
            lines.append(f"• <b>{sym}</b>  {age}  {vol:.1f}x  {mcap}")

        lines.append("")
        lines.append(f"Window: {self._tracker.max_age_hours}h")
        lines.append("/report SYMBOL for details")
        self._send(chat_id, "\n".join(lines))

    # ── /help ────────────────────────────────────────────────────────

    def _cmd_help(self, chat_id: str) -> None:
        text = (
            "🤖 <b>COMMANDS</b>\n\n"
            "/report — Quick performance overview\n"
            "/report ARC — Single coin detailed + diagnosis\n"
            "/analysis — Full backtesting report (all signals)\n"
            "/analysis ARC — Full report for one coin\n"
            "/summary — Stats + win rates\n"
            "/active — Quick signal list\n"
            "/help — This message\n\n"
            f"📡 Tracking window: {self._tracker.max_age_hours}h\n"
            "🏔 Prices update every 5 min\n"
            "🎯 Auto TP alerts at configured targets\n"
            "⚠️ Auto reversal warnings\n"
            "🔍 /report SYMBOL for diagnosis"
        )
        self._send(chat_id, text)
        
        
        
        #---------------------------------------------------------------------------------
        
        #ANALYSIS
            # ── /analysis (full backtesting report) ──────────────────────────

    def _cmd_analysis(self, chat_id: str, args: list) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        if not signals:
            self._send(chat_id, "🔬 No active signals.")
            return

        # optional filter by symbol
        if args:
            sym = args[0].upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            signals = [s for s in signals if s["symbol"] == sym]
            if not signals:
                self._send(chat_id, f"🔬 No active signals for <b>{sym}</b>")
                return

        signals.sort(key=lambda s: s["alert_time_ts"], reverse=True)

        for sig in signals:
            self._send_analysis_card(chat_id, sig, prices)
            time.sleep(0.5)

        # final summary
        if len(signals) > 1:
            self._send_analysis_summary(chat_id, signals, prices)

    def _send_analysis_card(self, chat_id: str, sig: dict, prices: dict) -> None:
        sym = sig["symbol"]
        entry = sig.get("entry_price", 0)
        highest = sig.get("highest_price", entry)
        current = prices.get(sym, sig.get("current_price", 0))
        if current > highest:
            highest = current

        cur_pct = self._calc_pct(entry, current) if entry > 0 else 0
        high_pct = self._calc_pct(entry, highest) if entry > 0 else 0
        age = self._fmt_age(sig["alert_time_ts"])
        result = self._result_emoji(cur_pct)

        lines = [
            f"{result} <b>{sym}</b>  —  {age}",
            "",
        ]

        # ── PRICE SECTION ────────────────────────────────────────────
        lines.append(f"Entry:   {self._fmt_price(entry)}")
        lines.append(f"Now:     {self._fmt_price(current)}  {self._fmt_pct(cur_pct)}")
        lines.append(f"Peak:    {self._fmt_price(highest)}  {self._fmt_pct(high_pct)}")

        # ── WHY BOT SENT THIS SIGNAL ─────────────────────────────────
        lines.append("")
        lines.append("── <b>WHY SIGNAL TRIGGERED</b> ──")

        # volume reason
        vol_ratio = sig.get("vol_ratio", 0)
        recent_fmt = sig.get("recent_vol_fmt", "")
        baseline_fmt = sig.get("baseline_vol_fmt", "")
        if recent_fmt and recent_fmt != "N/A":
            lines.append(
                f"📊 Volume {vol_ratio:.1f}x spike"
                f" ({recent_fmt} vs {baseline_fmt} normal)"
            )
        else:
            lines.append(f"📊 Volume {vol_ratio:.1f}x spike")

        # candle quality reason
        candle_color = sig.get("candle_color", "")
        body = sig.get("body_pct", 0)
        wick = sig.get("upper_wick_pct", 0)
        lwick = sig.get("lower_wick_pct", 0)
        if candle_color and body > 0:
            lines.append(
                f"🕯 {self._color_emoji(candle_color)} {candle_color} candle"
                f"  body:{body:.0f}%  wick:{wick:.0f}%  lwick:{lwick:.0f}%"
            )

        # breakout reason
        brk_margin = sig.get("breakout_margin_pct")
        brk_level = sig.get("breakout_level")
        if sig.get("breakout_confirmed") and brk_level:
            lines.append(
                f"🔺 Broke {self._fmt_price(brk_level)}"
                f" by +{brk_margin:.2f}%"
            )

        # OI reason
        oi = sig.get("oi_pct")
        if oi is not None:
            lines.append(f"📈 OI increased +{oi:.2f}%")

        # trend at time of signal
        pattern = sig.get("trend_pattern", "")
        trend_g = sig.get("trend_green", 0)
        trend_t = sig.get("trend_total", 0)
        if pattern:
            lines.append(
                f"📊 Trend: {trend_g}/{trend_t} green"
                f"  {self._pattern_emoji(pattern)}"
            )

        # market cap
        mcap = sig.get("mcap", "Unknown")
        lines.append(f"💰 MCap: {mcap}")

        # ── WHAT HAPPENED AFTER ──────────────────────────────────────
        lines.append("")
        lines.append("── <b>WHAT HAPPENED</b> ──")

        if entry > 0 and current > 0:
            if high_pct > 5 and cur_pct > 3:
                lines.append("🚀 Went up and holding — strong momentum")
            elif high_pct > 5 and cur_pct < 0:
                lines.append(
                    f"⚠️ Peaked at {high_pct:+.1f}% then reversed to {cur_pct:+.1f}%"
                )
                lines.append("   → Likely hit resistance / distribution")
            elif high_pct > 2 and cur_pct >= 0:
                lines.append("✅ Moderate move, still holding gains")
            elif high_pct < 1 and cur_pct < -3:
                lines.append("❌ Never moved up — signal failed")
                lines.append("   → May have been too late / weak setup")
            elif high_pct < 1 and cur_pct >= -3:
                lines.append("⏳ Flat — no significant move yet")
            elif cur_pct < -5:
                lines.append(f"🔴 Down {cur_pct:.1f}% — strong reversal against signal")
            else:
                lines.append(f"📊 Currently {cur_pct:+.1f}%, peaked at {high_pct:+.1f}%")

        # BTC context
        btc_at = sig.get("btc_price")
        btc_now = prices.get("BTCUSDT")
        if btc_at and btc_now:
            btc_chg = self._calc_pct(btc_at, btc_now)
            coin_vs_btc = cur_pct - btc_chg
            lines.append(
                f"₿ BTC {btc_chg:+.2f}% since signal"
                f"  →  coin vs BTC: {coin_vs_btc:+.2f}%"
            )

        # ── QUALITY FLAGS ────────────────────────────────────────────
        flags = self._quality_flags(sig, cur_pct, high_pct)
        if flags:
            lines.append("")
            lines.append("── <b>QUALITY FLAGS</b> ──")
            for f in flags:
                lines.append(f)

        lines.append("")
        lines.append(f"🕐 {sig.get('alert_time', 'N/A')}")
        lines.append("━" * 26)

        self._send(chat_id, "\n".join(lines))

    @staticmethod
    def _quality_flags(sig: dict, cur_pct: float, high_pct: float) -> list[str]:
        flags: list[str] = []

        wick = sig.get("upper_wick_pct", 0)
        body = sig.get("body_pct", 0)
        brk_margin = sig.get("breakout_margin_pct")
        trend_g = sig.get("trend_green", 0)
        trend_t = sig.get("trend_total", 5)
        vol_ratio = sig.get("vol_ratio", 0)
        candle_color = sig.get("candle_color", "")

        # positive flags
        if body >= 60:
            flags.append(f"✅ Strong body ({body:.0f}%)")
        if brk_margin is not None and brk_margin > 2:
            flags.append(f"✅ Clean breakout (+{brk_margin:.1f}%)")
        if trend_t > 0 and trend_g / trend_t >= 0.8:
            flags.append(f"✅ Strong trend ({trend_g}/{trend_t})")
        if vol_ratio >= 8:
            flags.append(f"✅ High volume ({vol_ratio:.0f}x)")
        oi = sig.get("oi_pct")
        if oi is not None and oi > 15:
            flags.append(f"✅ Strong OI (+{oi:.0f}%)")

        # warning flags
        if candle_color == "RED":
            flags.append("⚠️ RED candle")
        if 0 < body < 35:
            flags.append(f"⚠️ Weak body ({body:.0f}%)")
        if wick > 40:
            flags.append(f"⚠️ Large wick ({wick:.0f}%)")
        if brk_margin is not None and brk_margin < 0.5:
            flags.append(f"⚠️ Marginal breakout (+{brk_margin:.1f}%)")
        if trend_t > 0 and trend_g / trend_t < 0.4:
            flags.append(f"⚠️ Weak trend ({trend_g}/{trend_t})")
        if vol_ratio < 3.5:
            flags.append(f"⚠️ Low volume ({vol_ratio:.1f}x)")

        return flags

    def _send_analysis_summary(
        self, chat_id: str, signals: list, prices: dict
    ) -> None:
        valid = []
        for sig in signals:
            entry = sig.get("entry_price", 0)
            current = prices.get(sig["symbol"], sig.get("current_price", 0))
            highest = max(sig.get("highest_price", entry), current)
            if entry > 0 and current > 0:
                valid.append({
                    "symbol": sig["symbol"],
                    "cur_pct": self._calc_pct(entry, current),
                    "high_pct": self._calc_pct(entry, highest),
                    "vol_ratio": sig.get("vol_ratio", 0),
                    "body_pct": sig.get("body_pct", 0),
                    "trend_green": sig.get("trend_green", 0),
                    "trend_total": sig.get("trend_total", 0),
                    "brk_margin": sig.get("breakout_margin_pct"),
                    "oi_pct": sig.get("oi_pct"),
                })

        if not valid:
            return

        total = len(valid)
        winners = [v for v in valid if v["cur_pct"] > 0]
        losers = [v for v in valid if v["cur_pct"] <= 0]

        lines = [
            "🔬 <b>ANALYSIS SUMMARY</b>",
            "",
            f"Total signals: {total}",
            f"Winners: {len(winners)} ({len(winners)/total*100:.0f}%)",
            f"Losers:  {len(losers)} ({len(losers)/total*100:.0f}%)",
            "",
        ]

        # compare winners vs losers traits
        if winners and losers:
            lines.append("── <b>WINNER vs LOSER PATTERNS</b> ──")
            lines.append("")

            # average vol ratio
            w_vol = sum(v["vol_ratio"] for v in winners) / len(winners)
            l_vol = sum(v["vol_ratio"] for v in losers) / len(losers)
            lines.append(f"Avg volume:   W {w_vol:.1f}x  vs  L {l_vol:.1f}x")

            # average body (only those with data)
            w_body = [v["body_pct"] for v in winners if v["body_pct"] > 0]
            l_body = [v["body_pct"] for v in losers if v["body_pct"] > 0]
            if w_body and l_body:
                lines.append(
                    f"Avg body:     W {sum(w_body)/len(w_body):.0f}%"
                    f"  vs  L {sum(l_body)/len(l_body):.0f}%"
                )

            # average breakout margin
            w_brk = [v["brk_margin"] for v in winners if v["brk_margin"] is not None]
            l_brk = [v["brk_margin"] for v in losers if v["brk_margin"] is not None]
            if w_brk and l_brk:
                lines.append(
                    f"Avg breakout: W +{sum(w_brk)/len(w_brk):.1f}%"
                    f"  vs  L +{sum(l_brk)/len(l_brk):.1f}%"
                )

            # average trend
            w_trend = [
                v["trend_green"] / v["trend_total"]
                for v in winners if v["trend_total"] > 0
            ]
            l_trend = [
                v["trend_green"] / v["trend_total"]
                for v in losers if v["trend_total"] > 0
            ]
            if w_trend and l_trend:
                lines.append(
                    f"Avg trend:    W {sum(w_trend)/len(w_trend)*100:.0f}% green"
                    f"  vs  L {sum(l_trend)/len(l_trend)*100:.0f}% green"
                )

            # average OI
            w_oi = [v["oi_pct"] for v in winners if v["oi_pct"] is not None]
            l_oi = [v["oi_pct"] for v in losers if v["oi_pct"] is not None]
            if w_oi and l_oi:
                lines.append(
                    f"Avg OI:       W +{sum(w_oi)/len(w_oi):.1f}%"
                    f"  vs  L +{sum(l_oi)/len(l_oi):.1f}%"
                )

            lines.append("")
            lines.append("💡 Compare patterns to tune your config filters")

        self._send(chat_id, "\n".join(lines))