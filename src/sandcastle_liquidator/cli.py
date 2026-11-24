"""Command line: `status` prints every position's health once, `run` keeps watching."""

import argparse
import logging

from sandcastle_liquidator.bot import Bot
from sandcastle_liquidator.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="sandcastle-liquidator")
    parser.add_argument("command", choices=["status", "run"])
    command: str = parser.parse_args().command
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    bot = Bot(Settings.from_env())
    if command == "run":
        bot.run()
        return
    positions = bot.scan()
    print(f"{len(bot.discovery.markets)} markets, {len(bot.discovery.borrowers)} borrowers")
    print(f"{'health':>8}  {'debt':>26}  {'borrower':42}  core")
    for position in positions:
        flag = "  LIQUIDATABLE" if position.liquidatable else ""
        print(
            f"{position.health_factor:8.4f}  {position.debt:26d}  "
            f"{position.borrower}  {position.market.core}{flag}"
        )


if __name__ == "__main__":
    main()
