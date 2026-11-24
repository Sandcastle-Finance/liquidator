"""Plain data objects shared across the bot."""

from dataclasses import dataclass
from enum import IntEnum

from eth_typing import ChecksumAddress

WAD = 10**18
ORACLE_PRICE_SCALE = 10**36
VIRTUAL_SHARES = 10**6
VIRTUAL_ASSETS = 1
MAX_UINT256 = 2**256 - 1


class AmmType(IntEnum):
    """The FactoryFactory's `AmmType`, as emitted in `SetupDeployed`."""

    UNISWAP_V3 = 0
    SLIPSTREAM = 1
    UNISWAP_V4 = 2
    METRIC = 3


@dataclass(frozen=True)
class Factory:
    address: ChecksumAddress
    start_block: int


@dataclass(frozen=True)
class KyberSwapConfig:
    """KyberSwap's name for the chain, and the only router the bot will call for it."""

    chain: str
    router: ChecksumAddress


@dataclass(frozen=True)
class ChainConfig:
    chain_id: int
    name: str
    rpc_url: str
    hypersync_url: str
    max_log_range: int
    max_log_range_multi: int
    morpho: ChecksumAddress
    bundler3: ChecksumAddress
    general_adapter1: ChecksumAddress
    swap_adapter: ChecksumAddress
    universal_router: ChecksumAddress | None
    slipstream_router: ChecksumAddress | None
    multicall3: ChecksumAddress
    wrapped_native: ChecksumAddress
    kyberswap: KyberSwapConfig | None
    factories: tuple[Factory, ...]


@dataclass(frozen=True)
class Log:
    address: ChecksumAddress
    topics: tuple[bytes, ...]
    data: bytes
    block_number: int


@dataclass(frozen=True)
class LogQuery:
    """Logs with `topic0` from `addresses` in an inclusive block range."""

    topic0: bytes
    from_block: int
    to_block: int
    addresses: tuple[ChecksumAddress, ...]


@dataclass(frozen=True)
class CoreInfo:
    """The pool a core's positions sit in."""

    address: ChecksumAddress
    amm: AmmType
    token0: ChecksumAddress
    token1: ChecksumAddress
    fee: int
    tick_spacing: int
    hooks: ChecksumAddress
    adapter: ChecksumAddress
    pool_id: bytes


@dataclass(frozen=True)
class Market:
    """A Morpho market whose collateral and oracle are one Sandcastle core."""

    id: bytes
    core: ChecksumAddress
    loan_token: ChecksumAddress
    irm: ChecksumAddress
    lltv: int


@dataclass(frozen=True)
class Borrower:
    core: ChecksumAddress
    address: ChecksumAddress


@dataclass(frozen=True)
class MarketTotals:
    total_borrow_assets: int
    total_borrow_shares: int


@dataclass(frozen=True)
class MorphoPosition:
    borrow_shares: int
    collateral: int


@dataclass(frozen=True)
class PositionHealth:
    market: Market
    borrower: ChecksumAddress
    collateral: int
    borrow_shares: int
    debt: int
    price: int

    @property
    def max_borrow(self) -> int:
        """Morpho's borrow limit: collateral value at the oracle price, times LLTV."""
        return self.collateral * self.price // ORACLE_PRICE_SCALE * self.market.lltv // WAD

    @property
    def liquidatable(self) -> bool:
        return self.debt > self.max_borrow

    @property
    def health_factor(self) -> float:
        return self.max_borrow / self.debt if self.debt else float("inf")


def shares_to_assets_up(shares: int, totals: MarketTotals) -> int:
    """Morpho's `toAssetsUp`: the debt a liquidation or repayment is measured against."""
    numerator = shares * (totals.total_borrow_assets + VIRTUAL_ASSETS)
    denominator = totals.total_borrow_shares + VIRTUAL_SHARES
    return -(-numerator // denominator)
