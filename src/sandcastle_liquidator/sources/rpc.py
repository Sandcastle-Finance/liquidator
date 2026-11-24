from sandcastle_liquidator.chain import Chain
from sandcastle_liquidator.models import Log, LogQuery


class RpcLogSource:
    """`eth_getLogs`, split into the block spans the RPC accepts."""

    def __init__(self, chain: Chain) -> None:
        self.chain = chain
        self.max_range = chain.config.max_log_range
        self.max_range_multi = chain.config.max_log_range_multi

    def get_logs(self, query: LogQuery) -> list[Log]:
        """RPCs allow a far wider block span for one address than for several, so a query for
        several addresses goes out whichever way takes fewer requests: all the addresses
        together in narrow spans, or one address at a time in wide spans."""
        span = query.to_block - query.from_block + 1
        together = ceil_div(span, self.max_range_multi)
        one_by_one = len(query.addresses) * ceil_div(span, self.max_range)
        if len(query.addresses) > 1 and one_by_one < together:
            return [
                log
                for address in query.addresses
                for log in self.get_logs(
                    LogQuery(query.topic0, query.from_block, query.to_block, (address,))
                )
            ]
        step = self.max_range if len(query.addresses) == 1 else self.max_range_multi
        logs: list[Log] = []
        start = query.from_block
        while start <= query.to_block:
            end = min(start + step - 1, query.to_block)
            logs.extend(self.chain.get_logs(LogQuery(query.topic0, start, end, query.addresses)))
            start = end + 1
        return logs


def ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)
