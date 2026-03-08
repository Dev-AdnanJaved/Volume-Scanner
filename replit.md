# Binance Futures Volume Scanner Bot

A Python bot that monitors Binance USDT-M perpetual futures pairs for unusual trading activity and sends real-time alerts to Telegram.

## Features

- Volume spike detection (recent vs. baseline candle comparison)
- Price breakout confirmation
- Open Interest surge detection
- Market cap filtering via CoinGecko API
- Signal tracker with take-profit alerts and reversal detection
- Telegram bot commands for interactive queries
- Rate limit handling and caching

## Architecture

| File | Description |
|------|-------------|
| `main.py` | Entry point — loads config, starts threads |
| `scanner.py` | Core scanning loop and detection algorithms |
| `binance_client.py` | Binance Futures API wrapper with rate limiting |
| `notifier.py` | Telegram alert formatting and sending |
| `market_cap.py` | CoinGecko market cap fetch and caching |
| `tracker.py` | Background price updater + take-profit alerts |
| `bot_commands.py` | Telegram bot command handler |
| `config.json` | Configuration file (thresholds, scan settings) |

## Configuration

Settings live in `config.json`. Sensitive credentials are loaded from environment variables (which override `config.json` values):

- `TELEGRAM_BOT_TOKEN` — Telegram bot token from @BotFather
- `TELEGRAM_CHAT_ID` — Telegram channel/chat ID for alerts
- `BINANCE_API_KEY` — (optional) Binance API key
- `BINANCE_API_SECRET` — (optional) Binance API secret

## Running

The app runs as a console workflow (`python main.py`). It has no web frontend.

## Dependencies

- Python 3.11
- `requests>=2.31.0`
