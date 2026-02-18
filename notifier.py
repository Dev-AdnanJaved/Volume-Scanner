"""
Telegram Bot API helper.

• Validates the token on first use.
• Auto-retries on 429 (Telegram rate-limit).
• Formats rich HTML alert messages.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._session = requests.Session()
        self._ok = False

    # ── low level ────────────────────────────────────────────────────

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

    # ── high level ───────────────────────────────────────────────────

    def send_alert(self, data: dict) -> bool:
        return self.send(self._fmt_alert(data))

    def send_startup(self, summary: str) -> bool:
        return self.send(f"🤖 <b>Volume Scanner Started</b>\n\n{summary}\n\nScanner is now running …")

    # ── formatting ───────────────────────────────────────────────────

    @staticmethod
    def _fmt_alert(d: dict) -> str:
        # breakout status
        if not d.get("breakout_enabled"):
            brk = "⚫ Disabled"
        elif d.get("breakout_confirmed"):
            brk = "✅ Yes"
        else:
            brk = "❌ No"

        # OI status
        if not d.get("oi_enabled"):
            oi = "⚫ Disabled"
        elif d.get("oi_pct") is not None:
            pct = d["oi_pct"]
            icon = "📈" if pct >= 0 else "📉"
            oi = f"{icon} {pct:+.2f}%"
        else:
            oi = "⚠️ Data N/A"

        return (
            f"🚨 <b>VOLUME SPIKE ALERT</b>\n"
            f"{'━' * 28}\n\n"
            f"📌 <b>Symbol:</b>  {d['symbol']}\n"
            f"⏱  <b>Timeframe:</b>  {d['timeframe']}\n"
            f"💰 <b>Market Cap:</b>  {d['mcap']}\n"
            f"📊 <b>Vol Ratio:</b>  {d['vol_ratio']:.2f}x  "
            f"(threshold {d['vol_threshold']:.1f}x)\n"
            f"🔺 <b>Breakout:</b>  {brk}\n"
            f"📈 <b>OI Change:</b>  {oi}\n"
            f"💵 <b>Price:</b>  ${d.get('price', 'N/A')}\n"
            f"🕯  <b>Candle:</b>  {d['candle_time']}\n"
            f"🕐 <b>Sent:</b>  {d['alert_time']}\n"
        )