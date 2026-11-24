"""Ties discovery, health checks and liquidation together."""

import logging
import time

from sandcastle_liquidator.chain import Chain
from sandcastle_liquidator.config import LogSourceKind, Settings
from sandcastle_liquidator.discovery import Discovery
from sandcastle_liquidator.health import HealthChecker
from sandcastle_liquidator.liquidator import Liquidator
from sandcastle_liquidator.models import PositionHealth
from sandcastle_liquidator.sources.base import LogSource
from sandcastle_liquidator.sources.rpc import RpcLogSource

logger = logging.getLogger(__name__)


class MissingApiTokenError(Exception):
    pass


class Bot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.chain = Chain(settings.chain, settings.rpc_url)
        self.chain.verify()
        self.discovery = Discovery(self.chain, self.log_source())
        self.health = HealthChecker(self.chain)
        self.liquidator = Liquidator(self.chain, settings)

    def log_source(self) -> LogSource:
        if self.settings.log_source is LogSourceKind.RPC:
            return RpcLogSource(self.chain)
        if not self.settings.envio_api_token:
            raise MissingApiTokenError("LOG_SOURCE=hypersync needs ENVIO_API_TOKEN")
        # Imported here so the RPC path works without the optional dependency.
        from sandcastle_liquidator.sources.hypersync import HyperSyncLogSource

        return HyperSyncLogSource(self.settings.chain.hypersync_url, self.settings.envio_api_token)

    def scan(self) -> list[PositionHealth]:
        """Every position with debt, least healthy first."""
        self.discovery.sync(self.chain.block_number())
        pairs = [
            (market, borrower)
            for borrower in self.discovery.borrowers
            for market in self.discovery.markets_of(borrower.core)
        ]
        return sorted(self.health.check(pairs), key=lambda position: position.health_factor)

    def run(self) -> None:
        """Poll forever. A failed poll, such as an RPC outage, is logged and retried."""
        logger.info(
            "liquidator %s, %s",
            self.liquidator.executor.liquidator,
            "armed" if self.settings.armed else "dry run",
        )
        while True:
            try:
                self.poll()
            except Exception:
                logger.exception("poll failed")
            time.sleep(self.settings.poll_seconds)

    def poll(self) -> None:
        positions = self.scan()
        liquidatable = [position for position in positions if position.liquidatable]
        logger.info(
            "%d markets, %d positions with debt, %d liquidatable",
            len(self.discovery.markets),
            len(positions),
            len(liquidatable),
        )
        for position in liquidatable:
            self.liquidator.attempt(position, self.discovery.cores[position.market.core])
