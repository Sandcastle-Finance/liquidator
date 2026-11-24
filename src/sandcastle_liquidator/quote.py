"""Sizes a liquidation: what to seize, what to repay and how much to flash-borrow."""

from dataclasses import dataclass

from sandcastle_liquidator.models import ORACLE_PRICE_SCALE, WAD, PositionHealth

MAX_INCENTIVE = 115 * 10**16
INCENTIVE_CURSOR = 30 * 10**16
# Headroom on the flash loan for interest that accrues before the transaction lands.
INTEREST_BUFFER_BPS = 10


@dataclass(frozen=True)
class LiquidationPlan:
    """Arguments for the core's `liquidate`. Exactly one of `seized` and `repaid_shares` is
    non-zero, as Morpho requires. `expected_seized` is the collateral the liquidation takes
    either way."""

    position: PositionHealth
    seized: int
    repaid_shares: int
    expected_seized: int
    flash_amount: int


def incentive_factor(lltv: int) -> int:
    """Morpho's liquidation incentive: min(1.15, 1 / (1 - 0.3 * (1 - LLTV)))."""
    return min(MAX_INCENTIVE, WAD * WAD // (WAD - INCENTIVE_CURSOR * (WAD - lltv) // WAD))


def plan(position: PositionHealth) -> LiquidationPlan:
    """A full liquidation: repay all the debt, or seize all the collateral when the collateral
    cannot cover the debt plus the incentive. Either one satisfies the core's rule that
    remaining debt keeps at least 1% of the position's receipts behind it. Amounts are exact,
    so the transaction reverts if the position changed after it was read."""
    incentive = incentive_factor(position.market.lltv)
    seized_for_full_repay = position.debt * incentive // WAD * ORACLE_PRICE_SCALE // position.price
    if seized_for_full_repay <= position.collateral:
        return LiquidationPlan(
            position=position,
            seized=0,
            repaid_shares=position.borrow_shares,
            expected_seized=seized_for_full_repay,
            flash_amount=with_buffer(position.debt),
        )
    value = ceil_div(position.collateral * position.price, ORACLE_PRICE_SCALE)
    return LiquidationPlan(
        position=position,
        seized=position.collateral,
        repaid_shares=0,
        expected_seized=position.collateral,
        flash_amount=with_buffer(ceil_div(value * WAD, incentive)),
    )


def with_buffer(amount: int) -> int:
    return amount + ceil_div(amount * INTEREST_BUFFER_BPS, 10_000) + 1


def ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)
