"""
Telegram Bot API helper.

Sends:
  - Volume spike alerts (signal entry)
  - Take-profit target hit alerts
  - Reversal warning alerts
  - Startup summary
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._session = requests.Session()
        self._ok = False

    def _url(self, method: str) -> str:
        return self.API.format(token=self._token, method=method)

    def validate(self) -> bool:
        try:
            r = self._session.get(self._url("getMe"), timeout=10).json()
            if r.get("ok"):
                logger.info("Telegram bot validated: @%s", r["result"].get("username"))
                self._ok = True
                return True
            logger.error("Telegram validation failed: %s", r)
        except Exception as exc:
            logger.error("Telegram validation error: %s", exc)
        return False

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        for attempt in range(3):
            try:
                r = self._session.post(
                    self._url("sendMessage"),
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                ).json()
                if r.get("ok"):
                    return True
                if r.get("error_code") == 429:
                    wait = r.get("parameters", {}).get("retry_after", 30)
                    logger.warning("Telegram 429 — waiting %ds", wait)
                    time.sleep(wait)
                    continue
                logger.error("Telegram error: %s", r)
                return False
            except Exception as exc:
                logger.error("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2)
        return False

    # ── alert types ──────────────────────────────────────────────────

    def send_alert(self, data: dict) -> bool:
        return self.send(self._fmt_alert(data))

    def send_startup(self, summary: str) -> bool:
        return self.send(
            f"🤖 <b>Volume Scanner Started</b>\n\n{summary}\n\nScanner is now running …"
        )

    def send_take_profit(self, data: dict) -> bool:
        return self.send(self._fmt_take_profit(data))

    def send_reversal_warning(self, data: dict) -> bool:
        return self.send(self._fmt_reversal(data))

    # ── price formatting ─────────────────────────────────────────────

    @staticmethod
    def _fp(price: float) -> str:
        if price <= 0:
            return "N/A"
        if price >= 1000:
            return f"${price:,.2f}"
        if price >= 1:
            return f"${price:.4f}"
        if price >= 0.001:
            return f"${price:.6f}"
        return f"${price:.8f}"

    # ── signal alert format ──────────────────────────────────────────

    @staticmethod
    def _fmt_alert(d: dict) -> str:
        color_map = {"GREEN": "🟢", "RED": "🔴", "DOJI": "⚪"}
        candle_color = d.get("candle_color", "")
        color_icon = color_map.get(candle_color, "⚪")
        body = d.get("body_pct", 0)
        wick = d.get("upper_wick_pct", 0)

        vol_line = (
            f"📊 <b>Volume:</b>  {d['vol_ratio']:.2f}x  "
            f"({d.get('recent_vol_fmt', 'N/A')} vs {d.get('baseline_vol_fmt', 'N/A')} avg)"
        )

        candle_line = (
            f"🕯  <b>Candle:</b>   {color_icon} {candle_color}  |  "
            f"Body: {body:.0f}%  |  Wick: {wick:.0f}%"
        )

        if not d.get("breakout_enabled"):
            brk_line = "🔺 <b>Breakout:</b>  ⚫ Disabled"
        elif d.get("breakout_confirmed"):
            margin = d.get("breakout_margin_pct")
            level = d.get("breakout_level")
            if margin is not None and level is not None:
                lp = f"${level:.4f}" if level >= 1 else f"${level:.8f}"
                brk_line = f"🔺 <b>Breakout:</b>  ✅ +{margin:.2f}% above {lp}"
            else:
                brk_line = "🔺 <b>Breakout:</b>  ✅ Yes"
        else:
            brk_line = "🔺 <b>Breakout:</b>  ❌ No"

        if not d.get("oi_enabled"):
            oi_line = "📈 <b>OI Change:</b> ⚫ Disabled"
        elif d.get("oi_pct") is not None:
            pct = d["oi_pct"]
            icon = "📈" if pct >= 0 else "📉"
            oi_line = f"📈 <b>OI Change:</b> {icon} {pct:+.2f}%"
        else:
            oi_line = "📈 <b>OI Change:</b> ⚠️ Data N/A"

        pattern = d.get("trend_pattern", "")
        trend_g = d.get("trend_green", 0)
        trend_t = d.get("trend_total", 0)
        trend_line = ""
        if pattern:
            pe = pattern.replace("G", "🟢").replace("R", "🔴")
            trend_line = f"📊 <b>Trend:</b>    {trend_g}/{trend_t} green  {pe}"

        parts = [
            "🚨 <b>VOLUME SPIKE ALERT</b>",
            f"{'━' * 28}\n",
            f"📌 <b>Symbol:</b>    {d['symbol']}",
            f"⏱  <b>Timeframe:</b> {d['timeframe']}",
            f"💰 <b>Market Cap:</b> {d['mcap']}",
            f"💵 <b>Price:</b>     ${d.get('price', 'N/A')}",
            "",
            vol_line,
            candle_line,
            brk_line,
            oi_line,
        ]
        if trend_line:
            parts.append(trend_line)
        parts.extend(["", f"🕐 <b>Sent:</b>     {d['alert_time']}"])

        return "\n".join(parts)

    # ── take-profit alert format ─────────────────────────────────────

    def _fmt_take_profit(self, d: dict) -> str:
        target = d["target"]
        if target >= 15:
            icon = "🚀🚀"
        elif target >= 10:
            icon = "🚀"
        elif target >= 5:
            icon = "🎯"
        else:
            icon = "✅"

        cur_pct = d.get("cur_pct", 0)
        high_pct = d.get("high_pct", 0)
        age = d.get("age_str", "")

        return (
            f"{icon} <b>TARGET HIT  +{target}%</b>\n"
            f"{'━' * 28}\n\n"
            f"📌 <b>{d['symbol']}</b>\n"
            f"💵 Entry:    {self._fp(d['entry_price'])}\n"
            f"🏔  Peak:     {self._fp(d['highest_price'])}  (+{high_pct:.2f}%)\n"
            f"💵 Now:      {self._fp(d['current_price'])}  ({cur_pct:+.2f}%)\n"
            f"⏱  Age:      {age}\n\n"
            f"{'🟢 Still above target' if cur_pct >= target else '⚠️ Price pulled back from target'}"
        )

    # ── reversal warning format ──────────────────────────────────────

    def _fmt_reversal(self, d: dict) -> str:
        return (
            f"⚠️ <b>REVERSAL WARNING</b>\n"
            f"{'━' * 28}\n\n"
            f"📌 <b>{d['symbol']}</b>\n"
            f"💵 Entry:    {self._fp(d['entry_price'])}\n"
            f"🏔  Peak:     {self._fp(d['highest_price'])}  (+{d['high_pct']:.2f}%)\n"
            f"💵 Now:      {self._fp(d['current_price'])}  ({d['cur_pct']:+.2f}%)\n"
            f"📉 Drop:     {d['drop_pct']:.2f}% from peak\n"
            f"⏱  Age:      {d.get('age_str', '')}\n\n"
            f"Price has dropped significantly from its peak.\n"
            f"Consider taking remaining profits."
        )