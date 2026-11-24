"""Builds the one Bundler3 transaction that performs a flash-loan liquidation."""

from dataclasses import dataclass

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from sandcastle_liquidator.contracts import (
    Bundler3,
    Call,
    Core,
    Erc20,
    GeneralAdapter1,
    Route,
    SwapAdapter,
    SwapKind,
)
from sandcastle_liquidator.kyber import AggregatorSwap
from sandcastle_liquidator.models import (
    MAX_UINT256,
    AmmType,
    ChainConfig,
    CoreInfo,
    Market,
)
from sandcastle_liquidator.quote import LiquidationPlan

# Both sides of the pool price are shifted down so the squared sqrt price fits in a uint256.
PRICE_SHIFT = 64
PRICE_ONE = 1 << (192 - PRICE_SHIFT)
# How Uniswap V4 pools name native ETH.
NATIVE = to_checksum_address("0x" + "00" * 20)


@dataclass(frozen=True)
class Unsupported:
    """Why the bot cannot liquidate on a market yet."""

    reason: str


class BundleBuilder:
    def __init__(self, config: ChainConfig) -> None:
        self.config = config
        self.bundler = Bundler3(config.bundler3)
        self.general_adapter = GeneralAdapter1(config.general_adapter1)
        self.swap_adapter = SwapAdapter(config.swap_adapter)

    def route(self, core: CoreInfo, market: Market, sqrt_price_x96: int) -> Route | Unsupported:
        """The swap of the pool's other token into the loan token, through the same pool,
        with the pool's current price as the reference for slippage. In a Uniswap V4 pool the
        other token may be native ETH, which the SwapAdapter sells as the zero address."""
        if market.loan_token not in (core.token0, core.token1):
            return Unsupported("loan token is not one of the pool's tokens")
        router = self.config.universal_router
        kind = SwapKind.V3
        if core.amm is AmmType.METRIC:
            return Unsupported("Metric pools have no swap route")
        if core.amm is AmmType.SLIPSTREAM:
            router, kind = self.config.slipstream_router, SwapKind.SLIPSTREAM
        if core.amm is AmmType.UNISWAP_V4:
            kind = SwapKind.V4
        if router is None:
            return Unsupported(f"no router configured for {core.amm.name}")

        # token1 per token0, in raw units, is sqrtPrice^2 / 2^192.
        price = (sqrt_price_x96 * sqrt_price_x96) >> PRICE_SHIFT
        if price == 0:
            return Unsupported("pool price is out of range")
        selling_token0 = market.loan_token == core.token1
        return Route(
            router=router,
            source=core.token0 if selling_token0 else core.token1,
            destination=market.loan_token,
            kind=kind,
            fee=core.fee,
            tick_spacing=core.tick_spacing,
            hooks=core.hooks,
            reference_input=PRICE_ONE if selling_token0 else price,
            reference_output=price if selling_token0 else PRICE_ONE,
        )

    def pool_tokens(self, core: CoreInfo) -> tuple[ChecksumAddress, ChecksumAddress]:
        """The pool's tokens as ERC-20s: native ETH counts as its wrapped token."""
        token0 = self.config.wrapped_native if core.token0 == NATIVE else core.token0
        return token0, core.token1

    def build_pool(
        self,
        plan: LiquidationPlan,
        route: Route,
        liquidator: ChecksumAddress,
        slippage_bps: int,
        deadline: int,
    ) -> Call:
        """Liquidate and sell the pool's other token back through the same pool."""
        loan = plan.position.market.loan_token
        return self.flash_loan(
            plan,
            liquidator,
            [
                self.liquidate(plan, self.swap_adapter.address, deadline),
                self.swap_adapter.swap(route, MAX_UINT256, slippage_bps, deadline),
                self.swap_adapter.erc20_transfer(loan, liquidator, MAX_UINT256),
            ],
        )

    def build_aggregator(
        self,
        plan: LiquidationPlan,
        core: CoreInfo,
        swaps: list[AggregatorSwap],
        liquidator: ChecksumAddress,
        deadline: int,
    ) -> Call:
        """Liquidate and sell the redeemed tokens through an aggregator.

        The redeemed tokens land on GeneralAdapter1. For each swap, Bundler3 takes the exact
        input, approves the router for it and calls the router, which pays the liquidator.
        Whatever the swaps did not use is sent to the liquidator as it is.
        """
        loan = plan.position.market.loan_token
        adapter = self.general_adapter
        steps = [self.liquidate(plan, adapter.address, deadline)]
        if core.token0 == NATIVE:
            steps.append(adapter.wrap_native(MAX_UINT256, adapter.address).optional())
        for swap in swaps:
            steps += [
                adapter.erc20_transfer(swap.token_in, self.bundler.address, swap.amount_in),
                Erc20.approve(swap.token_in, swap.router, swap.amount_in),
                Call(swap.router, swap.data),
            ]
        steps += [
            adapter.erc20_transfer(token, liquidator, MAX_UINT256)
            for token in dict.fromkeys([*self.pool_tokens(core), loan])
        ]
        return self.flash_loan(plan, liquidator, steps)

    def liquidate(self, plan: LiquidationPlan, recipient: ChecksumAddress, deadline: int) -> Call:
        return Core.liquidate(
            plan.position.market,
            plan.position.borrower,
            plan.seized,
            plan.repaid_shares,
            plan.flash_amount,
            recipient,
            deadline,
        )

    def flash_loan(
        self, plan: LiquidationPlan, liquidator: ChecksumAddress, steps: list[Call]
    ) -> Call:
        """Flash-borrow the loan token, run `steps` and repay.

        The core pulls the repayment from the liquidator, so the flash loan is passed to the
        liquidator first and pulled back at the end. What is left with the liquidator is profit.
        """
        loan = plan.position.market.loan_token
        inside_flash_loan = [
            self.general_adapter.erc20_transfer(loan, liquidator, plan.flash_amount),
            *steps,
            self.general_adapter.erc20_transfer_from(
                loan, self.general_adapter.address, plan.flash_amount
            ),
        ]
        callback = Bundler3.callback(inside_flash_loan)
        flash_loan = self.general_adapter.morpho_flash_loan(loan, plan.flash_amount, callback)
        return self.bundler.multicall([flash_loan], [keccak(callback)])
