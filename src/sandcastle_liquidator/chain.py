"""RPC access: single calls and batched reads through Multicall3."""

from itertools import batched

from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from web3 import HTTPProvider, Web3
from web3.types import FilterParams, LogReceipt

from sandcastle_liquidator.contracts import Call, CallResult, Multicall3
from sandcastle_liquidator.models import ChainConfig, Log, LogQuery

MULTICALL_BATCH = 300


class ChainMismatchError(Exception):
    pass


class Chain:
    def __init__(self, config: ChainConfig, rpc_url: str) -> None:
        self.config = config
        self.web3 = Web3(HTTPProvider(rpc_url))
        self.multicall3 = Multicall3(config.multicall3)

    def verify(self) -> None:
        actual = self.web3.eth.chain_id
        if actual != self.config.chain_id:
            raise ChainMismatchError(f"RPC is chain {actual}, config is {self.config.chain_id}")

    def block_number(self) -> int:
        return self.web3.eth.block_number

    def call(self, call: Call) -> bytes:
        return bytes(self.web3.eth.call({"to": call.target, "data": call.data}))

    def multicall(self, calls: list[Call]) -> list[CallResult]:
        results: list[CallResult] = []
        for batch in batched(calls, MULTICALL_BATCH):
            returned = self.call(self.multicall3.aggregate(list(batch)))
            results.extend(Multicall3.decode_aggregate(returned))
        return results

    def get_logs(self, query: LogQuery) -> list[Log]:
        params: FilterParams = {
            "fromBlock": query.from_block,
            "toBlock": query.to_block,
            "topics": [HexBytes(query.topic0)],
            "address": list(query.addresses),
        }
        return [to_log(receipt) for receipt in self.web3.eth.get_logs(params)]


def to_log(receipt: LogReceipt) -> Log:
    address: ChecksumAddress = receipt["address"]
    return Log(
        address=address,
        topics=tuple(bytes(t) for t in receipt["topics"]),
        data=bytes(receipt["data"]),
        block_number=receipt["blockNumber"],
    )
