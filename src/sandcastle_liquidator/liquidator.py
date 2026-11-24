"""Takes one liquidatable position from plan to simulation to, when armed, a transaction."""

import logging
import time
from dataclasses import dataclass

from web3.exceptions import Web3Exception

from sandcastle_liquidator.bundle import BundleBuilder, Unsupported
from sandcastle_liquidator.chain import Chain
from sandcastle_liquidator.config import Settings
from sandcastle_liquidator.contracts import Call
from sandcastle_liquidator.executor import Executor, Reverted, TransactionFailedError
from sandcastle_liquidator.kyber import AggregatorSwap, KyberSwap, KyberSwapError
from sandcastle_liquidator.models import CoreInfo, PositionHealth
from sandcastle_liquidator.quote import LiquidationPlan, plan
from sandcastle_liquidator.redeem import Redeemed, RedeemEstimator

logger = logging.getLogger(__name__)
DEADLINE_SECONDS = 120
# Sold through the aggregator: the estimate less this, so the swap never asks for more
# than the liquidation redeems. The remainder reaches the liquidator unsold.
ESTIMATE_HAIRCUT_BPS = 10


@dataclass(frozen=True)
class Candidate:
    """One way to carry out a liquidation, and what its simulation left the liquidator."""

    route: str
    bundle: Call
    profit: int


class Liquidator:
    def __init__(self, chain: Chain, settings: Settings) -> None:
        self.chain = chain
        self.settings = settings
        self.builder = BundleBuilder(chain.config)
        self.estimator = RedeemEstimator(chain)
        self.executor = Executor(chain, settings.private_key)
        kyberswap = chain.config.kyberswap
        self.kyber = KyberSwap(kyberswap, settings.kyberswap_client_id) if kyberswap else None

    def attempt(self, position: PositionHealth, core: CoreInfo) -> None:
        market = position.market
        name = f"borrower {position.borrower} on core {market.core}"
        liquidation = plan(position)
        redeemed = self.estimator.estimate(core, liquidation)
        deadline = int(time.time()) + DEADLINE_SECONDS
        spenders = [market.core, self.chain.config.general_adapter1]

        candidates: list[Candidate] = []
        bundles = {
            "kyberswap": self.kyber_bundle(liquidation, core, redeemed, deadline),
            "pool": self.pool_bundle(liquidation, core, redeemed, deadline),
        }
        for route, bundle in bundles.items():
            if isinstance(bundle, Unsupported):
                logger.info("%s: no %s route: %s", name, route, bundle.reason)
                continue
            simulation = self.executor.simulate(bundle, market.loan_token, spenders)
            if isinstance(simulation, Reverted):
                logger.warning("%s: %s simulation reverted: %s", name, route, simulation.reason)
                continue
            logger.info("%s: %s simulated profit %d", name, route, simulation.profit)
            candidates.append(Candidate(route, bundle, simulation.profit))
        if not candidates:
            logger.warning("%s is liquidatable but no route works", name)
            return

        best = max(candidates, key=lambda candidate: candidate.profit)
        minimum = liquidation.flash_amount * self.settings.min_profit_bps // 10_000
        logger.info(
            "%s: flash %d, best route %s, profit %d (minimum %d) of loan token %s",
            name,
            liquidation.flash_amount,
            best.route,
            best.profit,
            minimum,
            market.loan_token,
        )
        if best.profit < minimum:
            return
        if not self.settings.armed or self.executor.account is None:
            logger.info("%s: dry run, not sending (set PRIVATE_KEY and ARM=1)", name)
            return
        try:
            for spender in spenders:
                self.executor.ensure_allowance(market.loan_token, spender)
            logger.info("%s: liquidated in %s", name, self.executor.send(best.bundle))
        except (Web3Exception, TransactionFailedError) as error:
            logger.warning("%s: transaction failed: %s", name, error)

    def pool_bundle(
        self, liquidation: LiquidationPlan, core: CoreInfo, redeemed: Redeemed, deadline: int
    ) -> Call | Unsupported:
        route = self.builder.route(core, liquidation.position.market, redeemed.sqrt_price_x96)
        if isinstance(route, Unsupported):
            return route
        return self.builder.build_pool(
            liquidation, route, self.executor.liquidator, self.settings.max_slippage_bps, deadline
        )

    def kyber_bundle(
        self, liquidation: LiquidationPlan, core: CoreInfo, redeemed: Redeemed, deadline: int
    ) -> Call | Unsupported:
        """Sell each redeemed token that is not the loan token through KyberSwap."""
        if self.kyber is None:
            return Unsupported("KyberSwap is not configured for this chain")
        loan = liquidation.position.market.loan_token
        amounts = zip(
            self.builder.pool_tokens(core), (redeemed.amount0, redeemed.amount1), strict=True
        )
        swaps: list[AggregatorSwap] = []
        try:
            for token, amount in amounts:
                amount_in = amount * (10_000 - ESTIMATE_HAIRCUT_BPS) // 10_000
                if token == loan or amount_in == 0:
                    continue
                swaps.append(
                    self.kyber.swap(
                        token_in=token,
                        token_out=loan,
                        amount_in=amount_in,
                        sender=self.chain.config.bundler3,
                        recipient=self.executor.liquidator,
                        slippage_bps=self.settings.max_slippage_bps,
                        deadline=deadline,
                    )
                )
        except KyberSwapError as error:
            return Unsupported(str(error))
        return self.builder.build_aggregator(
            liquidation, core, swaps, self.executor.liquidator, deadline
        )
