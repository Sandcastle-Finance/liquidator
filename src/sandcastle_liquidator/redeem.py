"""Estimates what a liquidation's seized collateral redeems into."""

from dataclasses import dataclass

from sandcastle_liquidator.chain import Chain
from sandcastle_liquidator.contracts import Core, PoolAdapter
from sandcastle_liquidator.models import CoreInfo
from sandcastle_liquidator.quote import LiquidationPlan


@dataclass(frozen=True)
class Redeemed:
    """Pool token amounts for the seized share of a position's liquidity, at the pool's
    current price. Uncollected fees come on top, so the real amounts are no smaller, short
    of rounding."""

    amount0: int
    amount1: int
    sqrt_price_x96: int


class RedeemEstimator:
    def __init__(self, chain: Chain) -> None:
        self.chain = chain

    def estimate(self, core: CoreInfo, plan: LiquidationPlan) -> Redeemed:
        held, state = self.chain.multicall(
            [
                Core.position(core.address, plan.position.borrower),
                PoolAdapter.pool_state(core.adapter, core.pool_id),
            ]
        )
        position = Core.decode_position(held.data)
        sqrt_price = PoolAdapter.decode_sqrt_price(state.data)
        amount0, amount1 = PoolAdapter.decode_amounts(
            self.chain.call(
                PoolAdapter.amounts_for_liquidity(core.adapter, position.token_id, sqrt_price)
            )
        )
        seized = min(plan.expected_seized, position.supply)
        return Redeemed(
            amount0=amount0 * seized // position.supply,
            amount1=amount1 * seized // position.supply,
            sqrt_price_x96=sqrt_price,
        )
