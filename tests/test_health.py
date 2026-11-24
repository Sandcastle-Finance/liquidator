from eth_utils.address import to_checksum_address

from sandcastle_liquidator.models import (
    Market,
    MarketTotals,
    PositionHealth,
    shares_to_assets_up,
)

ADDRESS = to_checksum_address("0x" + "11" * 20)
MARKET = Market(id=b"\x01" * 32, core=ADDRESS, loan_token=ADDRESS, irm=ADDRESS, lltv=86 * 10**16)


def position(debt: int) -> PositionHealth:
    # 100 receipts priced at 1 loan unit each: the borrow limit is 86 loan units.
    return PositionHealth(
        market=MARKET,
        borrower=ADDRESS,
        collateral=100 * 10**18,
        borrow_shares=1,
        debt=debt,
        price=10**36,
    )


def test_debt_at_the_limit_is_healthy() -> None:
    at_limit = position(86 * 10**18)
    assert at_limit.max_borrow == 86 * 10**18
    assert not at_limit.liquidatable
    assert at_limit.health_factor == 1.0


def test_debt_one_unit_over_the_limit_is_liquidatable() -> None:
    assert position(86 * 10**18 + 1).liquidatable


def test_shares_convert_to_assets_rounding_up() -> None:
    totals = MarketTotals(total_borrow_assets=1_000, total_borrow_shares=1_000 * 10**6)
    # 1 share of 1e9 + 1e6 total against 1001 assets is a fraction of a unit: rounds to 1.
    assert shares_to_assets_up(1, totals) == 1
    assert shares_to_assets_up(500 * 10**6, totals) == 500
