"""
Telegram Bot API helper.

Sends enriched alert messages with candle quality,
volume context, breakout margin, and trend data.
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

    def send_alert(self, data: dict) -> bool:
        return self.send(self._fmt_alert(data))

    def send_startup(self, summary: str) -> bool:
        return self.send(
            f"🤖 <b>Volume Scanner Started</b>\n\n{summary}\n\nScanner is now running …"
        )

    @staticmethod
    def _fmt_alert(d: dict) -> str:
        # candle quality
        color_map = {"GREEN": "🟢", "RED": "🔴", "DOJI": "⚪"}
        candle_color = d.get("candle_color", "")
        color_icon = color_map.get(candle_color, "⚪")
        body = d.get("body_pct", 0)
        wick = d.get("upper_wick_pct", 0)

        # volume
        vol_line = (
            f"📊 <b>Volume:</b>  {d['vol_ratio']:.2f}x  "
            f"({d.get('recent_vol_fmt', 'N/A')} vs {d.get('baseline_vol_fmt', 'N/A')} avg)"
        )

        # candle
        candle_line = (
            f"🕯  <b>Candle:</b>   {color_icon} {candle_color}  |  "
            f"Body: {body:.0f}%  |  Wick: {wick:.0f}%"
        )

        # breakout
        if not d.get("breakout_enabled"):
            brk_line = "🔺 <b>Breakout:</b>  ⚫ Disabled"
        elif d.get("breakout_confirmed"):
            margin = d.get("breakout_margin_pct")
            level = d.get("breakout_level")
            if margin is not None and level is not None:
                if level >= 1:
                    brk_line = f"🔺 <b>Breakout:</b>  ✅ +{margin:.2f}% above ${level:.4f}"
                else:
                    brk_line = f"🔺 <b>Breakout:</b>  ✅ +{margin:.2f}% above ${level:.8f}"
            else:
                brk_line = "🔺 <b>Breakout:</b>  ✅ Yes"
        else:
            brk_line = "🔺 <b>Breakout:</b>  ❌ No"

        # OI
        if not d.get("oi_enabled"):
            oi_line = "📈 <b>OI Change:</b> ⚫ Disabled"
        elif d.get("oi_pct") is not None:
            pct = d["oi_pct"]
            icon = "📈" if pct >= 0 else "📉"
            oi_line = f"📈 <b>OI Change:</b> {icon} {pct:+.2f}%"
        else:
            oi_line = "📈 <b>OI Change:</b> ⚠️ Data N/A"

        # trend
        pattern = d.get("trend_pattern", "")
        trend_g = d.get("trend_green", 0)
        trend_t = d.get("trend_total", 0)
        if pattern:
            pattern_emoji = pattern.replace("G", "🟢").replace("R", "🔴")
            trend_line = (
                f"📊 <b>Trend:</b>    {trend_g}/{trend_t} green  {pattern_emoji}"
            )
        else:
            trend_line = ""

        parts = [
            f"🚨 <b>VOLUME SPIKE ALERT</b>",
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

        parts.extend([
            "",
            f"🕐 <b>Sent:</b>     {d['alert_time']}",
        ])

        return "\n".join(parts)