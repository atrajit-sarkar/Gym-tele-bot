from __future__ import annotations

import asyncio
import logging

from app.bot import GymMotivationBot
from app.config import load_config
from dotenv import load_dotenv


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> None:
    load_dotenv()
    config = load_config()
    configure_logging(config.log_level)
    asyncio.set_event_loop(asyncio.new_event_loop())
    bot = GymMotivationBot(config)
    bot.run()


if __name__ == "__main__":
    main()
