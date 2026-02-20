"""
Binance USDT-M Futures Volume Scanner — Entry Point.

Starts three concurrent components:
  1. Scanner          — scans all pairs every cycle
  2. Signal Tracker   — background price updater + take-profit alerts
  3. Command Listener — Telegram bot command handler
"""

import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from binance_client import BinanceClient
from notifier import TelegramNotifier
from scanner import Scanner
from tracker import SignalTracker
from bot_commands import TelegramCommandListener


def load_config(path: str = "config.json") -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        print(f"ERROR  config file not found: {path}")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_cfg.get("log_file", "scanner.log"), encoding="utf-8"),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def validate_config(cfg: dict) -> None:
    required_keys = [
        ("telegram", "bot_token"),
        ("telegram", "chat_id"),
    ]
    for section, key in required_keys:
        value = cfg.get(section, {}).get(key, "")
        if not value or value.startswith("YOUR_"):
            logging.getLogger("main").error(
                "config.json  [%s][%s] is not set.", section, key
            )
            sys.exit(1)


def main() -> None:
    config = load_config()
    setup_logging(config)
    validate_config(config)
    logger = logging.getLogger("main")

    logger.info("=" * 60)
    logger.info("  Binance Futures Volume Scanner  —  starting")
    logger.info("=" * 60)

    # shared binance client
    rl = config.get("rate_limit", {})
    binance = BinanceClient(
        api_key=config["binance"].get("api_key", ""),
        api_secret=config["binance"].get("api_secret", ""),
        delay_ms=rl.get("binance_delay_ms", 100),
    )

    # shared telegram notifier
    notifier = TelegramNotifier(
        bot_token=config["telegram"]["bot_token"],
        chat_id=config["telegram"]["chat_id"],
    )
    if not notifier.validate():
        logger.error("Telegram validation failed — aborting.")
        sys.exit(1)

    # tracker (optional)
    tracker_cfg = config.get("tracker", {})
    tracker = None
    tracker_thread = None
    cmd_listener = None
    cmd_thread = None

    if tracker_cfg.get("enabled", False):
        tracker = SignalTracker(config, binance, notifier)

        tracker_thread = threading.Thread(
            target=tracker.run, name="tracker", daemon=True,
        )
        tracker_thread.start()

        cmd_listener = TelegramCommandListener(
            bot_token=config["telegram"]["bot_token"],
            chat_id=config["telegram"]["chat_id"],
            tracker=tracker,
            binance=binance,
        )
        cmd_thread = threading.Thread(
            target=cmd_listener.run, name="commands", daemon=True,
        )
        cmd_thread.start()
        logger.info("Tracker + command listener started")
    else:
        logger.info("Tracker disabled")

    # scanner (main thread)
    scanner = Scanner(config, binance, notifier, tracker)

    def _shutdown(sig, _frame):
        logger.info("Received signal %s — shutting down …", sig)
        scanner.stop()
        if tracker:
            tracker.stop()
        if cmd_listener:
            cmd_listener.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scanner.run()
    except Exception:
        logger.critical("Fatal error", exc_info=True)
        sys.exit(1)

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()