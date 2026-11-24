"""Reads each borrower's debt, collateral and price, and reports its health."""

from eth_typing import ChecksumAddress

from sandcastle_liquidator.chain import Chain
from sandcastle_liquidator.contracts import Core, Morpho
from sandcastle_liquidator.models import (
    Borrower,
    Market,
    MarketTotals,
    MorphoPosition,
    PositionHealth,
    shares_to_assets_up,
)


class HealthChecker:
    def __init__(self, chain: Chain) -> None:
        self.chain = chain
        self.morpho = Morpho(chain.config.morpho)

    def check(self, pairs: list[tuple[Market, Borrower]]) -> list[PositionHealth]:
        """Health of every pair that has debt. Debt uses Morpho's last accrued totals, so it
        slightly understates interest since the market's last interaction."""
        indebted = self.indebted(pairs)
        markets = list({market.id: market for market, _, _ in indebted}.values())
        totals = self.totals(markets)
        prices = self.chain.multicall(
            [Core.view_price(m.core, borrower, m.loan_token) for m, borrower, _ in indebted]
        )
        report: list[PositionHealth] = []
        for (market, borrower, position), price in zip(indebted, prices, strict=True):
            if not price.success:
                continue
            report.append(
                PositionHealth(
                    market=market,
                    borrower=borrower,
                    collateral=position.collateral,
                    borrow_shares=position.borrow_shares,
                    debt=shares_to_assets_up(position.borrow_shares, totals[market.id]),
                    price=Core.decode_price(price.data),
                )
            )
        return report

    def indebted(
        self, pairs: list[tuple[Market, Borrower]]
    ) -> list[tuple[Market, ChecksumAddress, MorphoPosition]]:
        results = self.chain.multicall(
            [self.morpho.position(market.id, borrower.address) for market, borrower in pairs]
        )
        found: list[tuple[Market, ChecksumAddress, MorphoPosition]] = []
        for (market, borrower), result in zip(pairs, results, strict=True):
            position = Morpho.decode_position(result.data)
            if position.borrow_shares:
                found.append((market, borrower.address, position))
        return found

    def totals(self, markets: list[Market]) -> dict[bytes, MarketTotals]:
        results = self.chain.multicall([self.morpho.market(market.id) for market in markets])
        return {
            market.id: Morpho.decode_market(result.data)
            for market, result in zip(markets, results, strict=True)
        }
