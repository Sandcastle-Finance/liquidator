from sandcastle_liquidator.chain import Chain, Endpoint
from sandcastle_liquidator.models import Log, LogQuery


class RpcLogSource:
    """`eth_getLogs`, split into the block spans the answering endpoint accepts."""

    def __init__(self, chain: Chain) -> None:
        self.chain = chain

    def get_logs(self, query: LogQuery) -> list[Log]:
        """A query that fails part-way on one endpoint starts again on the next, split to that
        endpoint's spans."""
        return self.chain.request(lambda endpoint: fetch(endpoint, query))


def fetch(endpoint: Endpoint, query: LogQuery) -> list[Log]:
    """RPCs allow a far wider block span for one address than for several, so a query for
    several addresses goes out whichever way takes fewer requests: all the addresses
    together in narrow spans, or one address at a time in wide spans."""
    limits = endpoint.config
    span = query.to_block - query.from_block + 1
    together = ceil_div(span, limits.max_log_range_multi)
    one_by_one = len(query.addresses) * ceil_div(span, limits.max_log_range)
    if len(query.addresses) > 1 and one_by_one < together:
        return [
            log
            for address in query.addresses
            for log in fetch(
                endpoint, LogQuery(query.topic0, query.from_block, query.to_block, (address,))
            )
        ]
    step = limits.max_log_range if len(query.addresses) == 1 else limits.max_log_range_multi
    logs: list[Log] = []
    start = query.from_block
    while start <= query.to_block:
        end = min(start + step - 1, query.to_block)
        logs.extend(endpoint.get_logs(LogQuery(query.topic0, start, end, query.addresses)))
        start = end + 1
    return logs


def ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)
