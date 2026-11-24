"""Typed call builders and decoders for the contracts the bot reads."""

from dataclasses import dataclass, replace
from enum import IntEnum

from eth_abi.abi import decode, encode
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from sandcastle_liquidator.models import (
    AmmType,
    CoreInfo,
    Log,
    Market,
    MarketTotals,
    MorphoPosition,
)


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def topic(signature: str) -> bytes:
    return keccak(text=signature)


def topic_address(value: bytes) -> ChecksumAddress:
    return to_checksum_address(value[-20:])


@dataclass(frozen=True)
class Call:
    target: ChecksumAddress
    data: bytes
    # Inside a Bundler3 bundle: carry on when this call reverts.
    skip_revert: bool = False

    def optional(self) -> "Call":
        return replace(self, skip_revert=True)


@dataclass(frozen=True)
class CallResult:
    success: bool
    data: bytes


@dataclass(frozen=True)
class MarketParams:
    loan_token: ChecksumAddress
    collateral_token: ChecksumAddress
    oracle: ChecksumAddress
    irm: ChecksumAddress
    lltv: int


@dataclass(frozen=True)
class CorePosition:
    """A borrower's wrapped LP position: the NFT and the receipts issued against it."""

    token_id: int
    supply: int


@dataclass(frozen=True)
class SetupDeployed:
    core: ChecksumAddress
    amm: AmmType
    market_id: bytes


@dataclass(frozen=True)
class PositionRegistered:
    core: ChecksumAddress
    borrower: ChecksumAddress


@dataclass(frozen=True)
class MarketRegistered:
    core: ChecksumAddress
    market_id: bytes


class Multicall3:
    AGGREGATE3 = selector("aggregate3((address,bool,bytes)[])")

    def __init__(self, address: ChecksumAddress) -> None:
        self.address = address

    def aggregate(self, calls: list[Call]) -> Call:
        """One call that runs `calls`, each allowed to fail on its own."""
        args = [(call.target, True, call.data) for call in calls]
        return Call(self.address, self.AGGREGATE3 + encode(["(address,bool,bytes)[]"], [args]))

    @staticmethod
    def decode_aggregate(data: bytes) -> list[CallResult]:
        (results,) = decode(["(bool,bytes)[]"], data)
        return [CallResult(success, returned) for success, returned in results]


class Morpho:
    POSITION = selector("position(bytes32,address)")
    MARKET = selector("market(bytes32)")
    ID_TO_MARKET_PARAMS = selector("idToMarketParams(bytes32)")

    def __init__(self, address: ChecksumAddress) -> None:
        self.address = address

    def position(self, market_id: bytes, borrower: ChecksumAddress) -> Call:
        return Call(
            self.address, self.POSITION + encode(["bytes32", "address"], [market_id, borrower])
        )

    @staticmethod
    def decode_position(data: bytes) -> MorphoPosition:
        _supply_shares, borrow_shares, collateral = decode(["uint256", "uint128", "uint128"], data)
        return MorphoPosition(borrow_shares=borrow_shares, collateral=collateral)

    def market(self, market_id: bytes) -> Call:
        return Call(self.address, self.MARKET + encode(["bytes32"], [market_id]))

    @staticmethod
    def decode_market(data: bytes) -> MarketTotals:
        _supply_assets, _supply_shares, borrow_assets, borrow_shares, _updated, _fee = decode(
            ["uint128"] * 6, data
        )
        return MarketTotals(total_borrow_assets=borrow_assets, total_borrow_shares=borrow_shares)

    def id_to_market_params(self, market_id: bytes) -> Call:
        return Call(self.address, self.ID_TO_MARKET_PARAMS + encode(["bytes32"], [market_id]))

    @staticmethod
    def decode_market_params(data: bytes) -> MarketParams:
        loan, collateral, oracle, irm, lltv = decode(
            ["address", "address", "address", "address", "uint256"], data
        )
        return MarketParams(
            loan_token=to_checksum_address(loan),
            collateral_token=to_checksum_address(collateral),
            oracle=to_checksum_address(oracle),
            irm=to_checksum_address(irm),
            lltv=lltv,
        )


class Core:
    """A Sandcastle PositionOracle: the collateral receipt and oracle of its markets."""

    VIEW_PRICE = selector("viewPrice(address,address)")
    POSITION_REGISTERED = topic("PositionRegistered(uint256,address,address,uint256)")
    MARKET_REGISTERED = topic("MarketRegistered(bytes32)")

    LIQUIDATE = selector(
        "liquidate((address,address,address,address,uint256),address,"
        "uint256,uint256,uint256,address,uint256,uint256,uint256)"
    )
    POOL_GETTERS = ("token0()", "token1()", "fee()", "tickSpacing()", "hookAddress()", "adapter()")
    POOL_ID = selector("poolId()")
    POSITIONS = selector("positions(address)")

    @classmethod
    def position(cls, core: ChecksumAddress, borrower: ChecksumAddress) -> Call:
        return Call(core, cls.POSITIONS + encode(["address"], [borrower]))

    @staticmethod
    def decode_position(data: bytes) -> CorePosition:
        _id, token_id, supply, _fee_bps = decode(["uint256", "uint256", "uint256", "uint16"], data)
        return CorePosition(token_id=token_id, supply=supply)

    @classmethod
    def info_calls(cls, core: ChecksumAddress) -> list[Call]:
        return [Call(core, selector(g)) for g in cls.POOL_GETTERS] + [Call(core, cls.POOL_ID)]

    @staticmethod
    def decode_info(core: ChecksumAddress, amm: AmmType, results: list[CallResult]) -> CoreInfo:
        token0, token1, fee, tick_spacing, hooks, adapter, pool_id = (r.data for r in results)
        return CoreInfo(
            address=core,
            amm=amm,
            token0=topic_address(token0),
            token1=topic_address(token1),
            fee=int.from_bytes(fee),
            tick_spacing=int.from_bytes(tick_spacing, signed=True),
            hooks=topic_address(hooks),
            adapter=topic_address(adapter),
            pool_id=pool_id,
        )

    @classmethod
    def liquidate(
        cls,
        market: Market,
        borrower: ChecksumAddress,
        seized: int,
        repaid_shares: int,
        max_loan_assets: int,
        recipient: ChecksumAddress,
        deadline: int,
    ) -> Call:
        """Liquidate through the core; the redeemed pool tokens go to `recipient`."""
        params = (market.loan_token, market.core, market.core, market.irm, market.lltv)
        args = encode(
            [
                "(address,address,address,address,uint256)",
                "address",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "uint256",
                "uint256",
                "uint256",
            ],
            [params, borrower, seized, repaid_shares, max_loan_assets, recipient, 0, 0, deadline],
        )
        return Call(market.core, cls.LIQUIDATE + args)

    @classmethod
    def view_price(
        cls, core: ChecksumAddress, borrower: ChecksumAddress, loan_token: ChecksumAddress
    ) -> Call:
        return Call(core, cls.VIEW_PRICE + encode(["address", "address"], [borrower, loan_token]))

    @staticmethod
    def decode_price(data: bytes) -> int:
        (price,) = decode(["uint256"], data)
        return price

    @staticmethod
    def decode_position_registered(log: Log) -> PositionRegistered:
        return PositionRegistered(core=log.address, borrower=topic_address(log.topics[2]))

    @staticmethod
    def decode_market_registered(log: Log) -> MarketRegistered:
        return MarketRegistered(core=log.address, market_id=log.topics[1])


class FactoryFactory:
    SETUP_DEPLOYED = topic("SetupDeployed(uint256,address,uint8,address,bytes32,bytes32,address)")

    @staticmethod
    def decode_setup_deployed(log: Log) -> SetupDeployed:
        _adapter, market_id, _pool_id, _creator = decode(
            ["address", "bytes32", "bytes32", "address"], log.data
        )
        return SetupDeployed(
            core=topic_address(log.topics[2]),
            amm=AmmType(int.from_bytes(log.topics[3])),
            market_id=market_id,
        )


class PoolAdapter:
    """A core's AMM adapter, read for the pool's price and a position's contents."""

    GET_POOL_STATE = selector("getPoolState(bytes32)")
    GET_AMOUNTS_FOR_LIQUIDITY = selector("getAmountsForLiquidity(uint256,uint160)")

    @classmethod
    def amounts_for_liquidity(
        cls, adapter: ChecksumAddress, token_id: int, sqrt_price_x96: int
    ) -> Call:
        """The token amounts a position's whole liquidity is worth at a pool price."""
        args = encode(["uint256", "uint160"], [token_id, sqrt_price_x96])
        return Call(adapter, cls.GET_AMOUNTS_FOR_LIQUIDITY + args)

    @staticmethod
    def decode_amounts(data: bytes) -> tuple[int, int]:
        amount0, amount1 = decode(["uint256", "uint256"], data)
        return amount0, amount1

    @classmethod
    def pool_state(cls, adapter: ChecksumAddress, pool_id: bytes) -> Call:
        return Call(adapter, cls.GET_POOL_STATE + encode(["bytes32"], [pool_id]))

    @staticmethod
    def decode_sqrt_price(data: bytes) -> int:
        sqrt_price_x96, _tick = decode(["uint160", "int24"], data)
        return sqrt_price_x96


class Erc20:
    BALANCE_OF = selector("balanceOf(address)")
    ALLOWANCE = selector("allowance(address,address)")
    APPROVE = selector("approve(address,uint256)")

    @classmethod
    def balance_of(cls, token: ChecksumAddress, owner: ChecksumAddress) -> Call:
        return Call(token, cls.BALANCE_OF + encode(["address"], [owner]))

    @classmethod
    def allowance(
        cls, token: ChecksumAddress, owner: ChecksumAddress, spender: ChecksumAddress
    ) -> Call:
        return Call(token, cls.ALLOWANCE + encode(["address", "address"], [owner, spender]))

    @classmethod
    def approve(cls, token: ChecksumAddress, spender: ChecksumAddress, amount: int) -> Call:
        return Call(token, cls.APPROVE + encode(["address", "uint256"], [spender, amount]))

    @staticmethod
    def decode_amount(data: bytes) -> int:
        (amount,) = decode(["uint256"], data)
        return amount


class SwapKind(IntEnum):
    """The SwapAdapter's `Kind`."""

    UNISWAP_V2 = 0
    V3 = 1
    V4 = 2
    SLIPSTREAM = 3


@dataclass(frozen=True)
class Route:
    """One swap. The minimum output is `amount * reference_output / reference_input`, less
    the slippage passed with the swap."""

    router: ChecksumAddress
    source: ChecksumAddress
    destination: ChecksumAddress
    kind: SwapKind
    fee: int
    tick_spacing: int
    hooks: ChecksumAddress
    reference_input: int
    reference_output: int


class TransferAdapter:
    """What every Bundler3 adapter inherits: sending out the tokens it holds."""

    ERC20_TRANSFER = selector("erc20Transfer(address,address,uint256)")

    def __init__(self, address: ChecksumAddress) -> None:
        self.address = address

    def erc20_transfer(
        self, token: ChecksumAddress, receiver: ChecksumAddress, amount: int
    ) -> Call:
        args = encode(["address", "address", "uint256"], [token, receiver, amount])
        return Call(self.address, self.ERC20_TRANSFER + args)


class GeneralAdapter1(TransferAdapter):
    MORPHO_FLASH_LOAN = selector("morphoFlashLoan(address,uint256,bytes)")
    ERC20_TRANSFER_FROM = selector("erc20TransferFrom(address,address,uint256)")
    WRAP_NATIVE = selector("wrapNative(uint256,address)")

    def wrap_native(self, amount: int, receiver: ChecksumAddress) -> Call:
        """Wrap native tokens the adapter holds; the maximum amount wraps its whole balance."""
        return Call(
            self.address, self.WRAP_NATIVE + encode(["uint256", "address"], [amount, receiver])
        )

    def morpho_flash_loan(self, token: ChecksumAddress, assets: int, data: bytes) -> Call:
        args = encode(["address", "uint256", "bytes"], [token, assets, data])
        return Call(self.address, self.MORPHO_FLASH_LOAN + args)

    def erc20_transfer_from(
        self, token: ChecksumAddress, receiver: ChecksumAddress, amount: int
    ) -> Call:
        """Pull `amount` from the bundle's initiator, who must have approved this adapter."""
        args = encode(["address", "address", "uint256"], [token, receiver, amount])
        return Call(self.address, self.ERC20_TRANSFER_FROM + args)


class SwapAdapter(TransferAdapter):
    ROUTE = "(address,address,address,uint8,uint24,int24,address,uint256,uint256)"
    SWAP = selector(f"swap({ROUTE},uint256,uint256,uint256)")

    def swap(self, route: Route, amount: int, slippage_bps: int, deadline: int) -> Call:
        fields = (
            route.router,
            route.source,
            route.destination,
            int(route.kind),
            route.fee,
            route.tick_spacing,
            route.hooks,
            route.reference_input,
            route.reference_output,
        )
        args = encode(
            [self.ROUTE, "uint256", "uint256", "uint256"], [fields, amount, slippage_bps, deadline]
        )
        return Call(self.address, self.SWAP + args)


class Bundler3:
    BUNDLE = "(address,bytes,uint256,bool,bytes32)[]"
    MULTICALL = selector(f"multicall({BUNDLE})")
    NO_CALLBACK = bytes(32)

    def __init__(self, address: ChecksumAddress) -> None:
        self.address = address

    @classmethod
    def encode_bundle(cls, calls: list[Call], callback_hashes: list[bytes]) -> bytes:
        steps = [
            (call.target, call.data, 0, call.skip_revert, callback_hash)
            for call, callback_hash in zip(calls, callback_hashes, strict=True)
        ]
        return encode([cls.BUNDLE], [steps])

    def multicall(self, calls: list[Call], callback_hashes: list[bytes]) -> Call:
        return Call(self.address, self.MULTICALL + self.encode_bundle(calls, callback_hashes))

    @classmethod
    def callback(cls, calls: list[Call]) -> bytes:
        """Calldata an adapter hands back to Bundler3 from inside a Morpho callback."""
        return cls.encode_bundle(calls, [cls.NO_CALLBACK] * len(calls))
