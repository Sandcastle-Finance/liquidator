"""Finds every Sandcastle core, market and borrower from event logs."""

from eth_typing import ChecksumAddress

from sandcastle_liquidator.chain import Chain
from sandcastle_liquidator.contracts import Core, FactoryFactory, Morpho
from sandcastle_liquidator.models import AmmType, Borrower, CoreInfo, Log, LogQuery, Market
from sandcastle_liquidator.sources.base import LogSource


class Discovery:
    """Keeps the known markets and borrowers current; each `sync` reads only new blocks."""

    def __init__(self, chain: Chain, source: LogSource) -> None:
        self.chain = chain
        self.source = source
        self.morpho = Morpho(chain.config.morpho)
        self.factories = tuple(f.address for f in chain.config.factories)
        self.next_block = min(f.start_block for f in chain.config.factories)
        self.cores: dict[ChecksumAddress, CoreInfo] = {}
        self.markets: dict[bytes, Market] = {}
        self.borrowers: set[Borrower] = set()

    def sync(self, head: int) -> None:
        if head < self.next_block:
            return
        start = self.next_block
        market_ids: dict[bytes, ChecksumAddress] = {}
        new_cores: dict[ChecksumAddress, AmmType] = {}

        setups = self.source.get_logs(
            LogQuery(FactoryFactory.SETUP_DEPLOYED, start, head, self.factories)
        )
        for log in setups:
            setup = FactoryFactory.decode_setup_deployed(log)
            market_ids[setup.market_id] = setup.core
            if setup.core not in self.cores and setup.core not in new_cores:
                new_cores[setup.core] = setup.amm
        # A core has no logs before the block that deployed it, so one range covers them all.
        core_logs = self.core_logs(start, head, (*self.cores, *new_cores))
        self.load_cores(new_cores)

        for log in core_logs:
            if log.topics[0] == Core.MARKET_REGISTERED:
                registered = Core.decode_market_registered(log)
                market_ids[registered.market_id] = registered.core
            else:
                position = Core.decode_position_registered(log)
                self.borrowers.add(Borrower(position.core, position.borrower))

        self.load_markets({i: c for i, c in market_ids.items() if i not in self.markets})
        self.next_block = head + 1

    def core_logs(self, start: int, head: int, cores: tuple[ChecksumAddress, ...]) -> list[Log]:
        if not cores:
            return []
        return [
            *self.source.get_logs(LogQuery(Core.MARKET_REGISTERED, start, head, cores)),
            *self.source.get_logs(LogQuery(Core.POSITION_REGISTERED, start, head, cores)),
        ]

    def load_cores(self, cores: dict[ChecksumAddress, AmmType]) -> None:
        calls = [call for core in cores for call in Core.info_calls(core)]
        results = self.chain.multicall(calls)
        per_core = len(calls) // len(cores) if cores else 0
        for index, (core, amm) in enumerate(cores.items()):
            chunk = results[index * per_core : (index + 1) * per_core]
            self.cores[core] = Core.decode_info(core, amm, chunk)

    def load_markets(self, market_ids: dict[bytes, ChecksumAddress]) -> None:
        ids = list(market_ids)
        results = self.chain.multicall([self.morpho.id_to_market_params(i) for i in ids])
        for market_id, result in zip(ids, results, strict=True):
            params = Morpho.decode_market_params(result.data)
            core = market_ids[market_id]
            # A market only counts when Morpho agrees the core is its collateral and oracle.
            if params.collateral_token != core or params.oracle != core:
                continue
            self.markets[market_id] = Market(
                id=market_id,
                core=core,
                loan_token=params.loan_token,
                irm=params.irm,
                lltv=params.lltv,
            )

    def markets_of(self, core: ChecksumAddress) -> list[Market]:
        return [market for market in self.markets.values() if market.core == core]
