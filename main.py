"""
Binance USDT-M Futures Volume Scanner — Entry Point.

Scans perpetual futures for volume anomalies, optional breakout
confirmation, optional open-interest surge, and alerts via Telegram.
"""

import json
import logging
import signal
import sys
import time
from pathlib import Path

from scanner import Scanner


# ── helpers ──────────────────────────────────────────────────────────

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
    """Fail fast if critical keys are missing."""
    required_keys = [
        ("telegram", "bot_token"),
        ("telegram", "chat_id"),
    ]
    for section, key in required_keys:
        value = cfg.get(section, {}).get(key, "")
        if not value or value.startswith("YOUR_"):
            logging.getLogger("main").error(
                "config.json  [%s][%s] is not set — please fill it in.", section, key
            )
            sys.exit(1)


# ── main ─────────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()
    setup_logging(config)
    validate_config(config)
    logger = logging.getLogger("main")

    logger.info("=" * 60)
    logger.info("  Binance Futures Volume Scanner  —  starting")
    logger.info("=" * 60)

    scanner = Scanner(config)

    # graceful shutdown on Ctrl-C / SIGTERM
    def _shutdown(sig, _frame):
        logger.info("Received signal %s — shutting down …", sig)
        scanner.stop()
        sys.exit(0) 

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scanner.run()
    except Exception:
        logger.critical("Fatal error", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()