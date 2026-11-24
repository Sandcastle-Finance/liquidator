from pathlib import Path

from eth_abi.abi import decode
from eth_utils.address import to_checksum_address

from sandcastle_liquidator.bundle import NATIVE, BundleBuilder
from sandcastle_liquidator.config import load_chain
from sandcastle_liquidator.contracts import Bundler3, Erc20, GeneralAdapter1
from sandcastle_liquidator.kyber import AggregatorSwap
from sandcastle_liquidator.models import AmmType, CoreInfo, Market, PositionHealth
from sandcastle_liquidator.quote import plan

CONFIG = load_chain(Path(__file__).parents[1] / "config" / "robinhood.toml")
LOAN, OTHER, CORE, LIQUIDATOR, ROUTER = (
    to_checksum_address("0x" + f"{byte:02x}" * 20) for byte in range(1, 6)
)
MARKET = Market(id=b"\x01" * 32, core=CORE, loan_token=LOAN, irm=CORE, lltv=86 * 10**16)
POSITION = PositionHealth(
    market=MARKET,
    borrower=CORE,
    collateral=100 * 10**18,
    borrow_shares=7,
    debt=90 * 10**18,
    price=10**36,
)


def core(token0: str) -> CoreInfo:
    return CoreInfo(
        address=CORE,
        amm=AmmType.UNISWAP_V4,
        token0=to_checksum_address(token0),
        token1=LOAN,
        fee=3000,
        tick_spacing=60,
        hooks=NATIVE,
        adapter=CORE,
        pool_id=b"\x02" * 32,
    )


def steps(bundle_data: bytes) -> list[tuple[str, bytes, bool]]:
    """The calls that run inside the flash loan, as (target, calldata, skip_revert)."""
    ((flash_loan,),) = decode([Bundler3.BUNDLE], bundle_data[4:])
    _token, _assets, callback = decode(["address", "uint256", "bytes"], flash_loan[1][4:])
    (inner,) = decode([Bundler3.BUNDLE], callback)
    return [(to_checksum_address(to), data, skip) for to, data, _value, skip, _hash in inner]


def test_aggregator_bundle_swaps_from_bundler_and_sweeps_leftovers() -> None:
    builder = BundleBuilder(CONFIG)
    swap = AggregatorSwap(
        router=ROUTER, token_in=OTHER, amount_in=5, amount_out=9, data=b"\xaa\xbb"
    )
    bundle = builder.build_aggregator(plan(POSITION), core(OTHER), [swap], LIQUIDATOR, 0)
    inner = steps(bundle.data)

    adapter = CONFIG.general_adapter1
    assert [target for target, _, _ in inner] == [
        adapter,  # flash loan to the liquidator
        CORE,  # liquidate, redeeming to the adapter
        adapter,  # swap input to Bundler3
        OTHER,  # approve the router
        ROUTER,  # swap
        adapter,  # sweeps
        adapter,
        adapter,  # pull the flash loan back
    ]
    assert inner[2][1] == GeneralAdapter1(adapter).erc20_transfer(OTHER, CONFIG.bundler3, 5).data
    assert inner[3][1] == Erc20.approve(OTHER, ROUTER, 5).data
    assert inner[4][1] == b"\xaa\xbb"
    assert not any(skip for _, _, skip in inner)


def test_native_pool_token_is_wrapped_before_swapping() -> None:
    builder = BundleBuilder(CONFIG)
    native = core(NATIVE)
    assert builder.pool_tokens(native) == (CONFIG.wrapped_native, LOAN)
    inner = steps(builder.build_aggregator(plan(POSITION), native, [], LIQUIDATOR, 0).data)
    wrap = inner[2]
    assert wrap[1][:4] == GeneralAdapter1.WRAP_NATIVE
    assert wrap[2]  # nothing to wrap must not fail the bundle
