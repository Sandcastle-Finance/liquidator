from eth_utils.address import to_checksum_address

from sandcastle_liquidator.models import Market, PositionHealth
from sandcastle_liquidator.quote import incentive_factor, plan

ADDRESS = to_checksum_address("0x" + "11" * 20)
MARKET = Market(id=b"\x01" * 32, core=ADDRESS, loan_token=ADDRESS, irm=ADDRESS, lltv=86 * 10**16)


def position(debt: int) -> PositionHealth:
    # 100 receipts worth 100 loan units; the incentive at 86% LLTV is about 4.38%.
    return PositionHealth(
        market=MARKET,
        borrower=ADDRESS,
        collateral=100 * 10**18,
        borrow_shares=7,
        debt=debt,
        price=10**36,
    )


def test_incentive_matches_morpho() -> None:
    assert incentive_factor(86 * 10**16) == 1043841336116910229
    assert incentive_factor(50 * 10**16) == 115 * 10**16


def test_repays_all_debt_when_collateral_covers_the_incentive() -> None:
    liquidation = plan(position(90 * 10**18))
    assert (liquidation.seized, liquidation.repaid_shares) == (0, 7)
    assert liquidation.flash_amount > 90 * 10**18


def test_seizes_all_collateral_when_it_cannot_cover_the_incentive() -> None:
    liquidation = plan(position(99 * 10**18))
    assert (liquidation.seized, liquidation.repaid_shares) == (100 * 10**18, 0)
    # Repays the collateral's value divided by the incentive: less than the debt.
    assert 95 * 10**18 < liquidation.flash_amount < 97 * 10**18
