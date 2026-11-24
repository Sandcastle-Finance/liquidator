import asyncio

import hypersync
from eth_utils.address import to_checksum_address

from sandcastle_liquidator.models import Log, LogQuery

LOG_FIELDS = [
    hypersync.LogField.ADDRESS,
    hypersync.LogField.BLOCK_NUMBER,
    hypersync.LogField.DATA,
    hypersync.LogField.TOPIC0,
    hypersync.LogField.TOPIC1,
    hypersync.LogField.TOPIC2,
    hypersync.LogField.TOPIC3,
]


class IncompleteLogError(Exception):
    pass


class HyperSyncLogSource:
    """Envio HyperSync: the same logs as the RPC, served from an indexed archive."""

    def __init__(self, url: str, api_token: str) -> None:
        self.client = hypersync.HypersyncClient(
            hypersync.ClientConfig(url=url, bearer_token=api_token)
        )

    def get_logs(self, query: LogQuery) -> list[Log]:
        return asyncio.run(self.fetch(query))

    async def fetch(self, query: LogQuery) -> list[Log]:
        selection = hypersync.LogSelection(
            address=list(query.addresses),
            topics=[["0x" + query.topic0.hex()]],
        )
        logs: list[Log] = []
        next_block = query.from_block
        # HyperSync's to_block is exclusive and a response may stop early; follow next_block.
        while next_block <= query.to_block:
            response = await self.client.get(
                hypersync.Query(
                    from_block=next_block,
                    to_block=query.to_block + 1,
                    logs=[selection],
                    field_selection=hypersync.FieldSelection(log=LOG_FIELDS),
                )
            )
            logs.extend(to_log(log) for log in response.data.logs)
            if response.next_block <= next_block:
                break
            next_block = response.next_block
        return logs


def to_log(log: hypersync.Log) -> Log:
    if log.address is None or log.block_number is None or log.topics is None:
        raise IncompleteLogError("HyperSync returned a log without the selected fields")
    topics: list[str | None] = list(log.topics)
    return Log(
        address=to_checksum_address(log.address),
        topics=tuple(bytes.fromhex(t.removeprefix("0x")) for t in topics if t),
        data=bytes.fromhex((log.data or "0x").removeprefix("0x")),
        block_number=log.block_number,
    )
