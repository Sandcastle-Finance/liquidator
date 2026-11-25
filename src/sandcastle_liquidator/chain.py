"""RPC access: single calls and batched reads through Multicall3, over one or more endpoints."""

import logging
import time
from collections.abc import Callable
from itertools import batched
from urllib.parse import urlsplit

from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from requests.exceptions import RequestException
from web3 import HTTPProvider, Web3
from web3.exceptions import Web3RPCError
from web3.types import FilterParams, LogReceipt

from sandcastle_liquidator.contracts import Call, CallResult, Multicall3
from sandcastle_liquidator.models import ChainConfig, Log, LogQuery, RpcEndpoint

logger = logging.getLogger(__name__)

MULTICALL_BATCH = 300
REQUEST_TIMEOUT_SECONDS = 10
# How long a failed endpoint is passed over before it is tried again.
COOLDOWN_SECONDS = 60
# What an endpoint that is down, throttling or refusing a request raises. A revert is a
# `ContractLogicError`, which is neither: it is the answer, and no other endpoint is asked.
ENDPOINT_ERRORS = (RequestException, Web3RPCError)


class ChainMismatchError(Exception):
    pass


class RpcUnavailableError(Exception):
    """Every endpoint failed the request."""


class Endpoint:
    """One RPC endpoint. `host` names it in logs; the URL can hold an API key."""

    def __init__(self, config: RpcEndpoint) -> None:
        self.config = config
        self.host = urlsplit(config.url).hostname or "rpc"
        self.web3 = Web3(
            HTTPProvider(config.url, request_kwargs={"timeout": REQUEST_TIMEOUT_SECONDS})
        )
        self.retry_at = 0.0

    def get_logs(self, query: LogQuery) -> list[Log]:
        params: FilterParams = {
            "fromBlock": query.from_block,
            "toBlock": query.to_block,
            "topics": [HexBytes(query.topic0)],
            "address": list(query.addresses),
        }
        return [to_log(receipt) for receipt in self.web3.eth.get_logs(params)]


class Chain:
    def __init__(self, config: ChainConfig) -> None:
        self.config = config
        self.endpoints = [Endpoint(rpc) for rpc in config.rpcs]
        self.multicall3 = Multicall3(config.multicall3)

    def request[T](self, send: Callable[[Endpoint], T]) -> T:
        """Runs `send` on the first endpoint that answers, in the configured order. An endpoint
        that fails is passed over for a while, so an outage does not slow every request."""
        now = time.monotonic()
        ready = [endpoint for endpoint in self.endpoints if endpoint.retry_at <= now]
        failures: list[str] = []
        for endpoint in ready or self.endpoints:
            try:
                return send(endpoint)
            except ENDPOINT_ERRORS as error:
                endpoint.retry_at = time.monotonic() + COOLDOWN_SECONDS
                failures.append(f"{endpoint.host}: {describe(error)}")
                logger.warning("RPC %s failed: %s", endpoint.host, describe(error))
        # Raised without the cause: a transport error's text holds the URL, key included.
        raise RpcUnavailableError("; ".join(failures)) from None

    def verify(self) -> None:
        """Checks the chain id of every endpoint that answers. One must."""
        answered = 0
        for endpoint in self.endpoints:
            try:
                actual = endpoint.web3.eth.chain_id
            except ENDPOINT_ERRORS as error:
                logger.warning("RPC %s failed: %s", endpoint.host, describe(error))
                continue
            if actual != self.config.chain_id:
                raise ChainMismatchError(
                    f"{endpoint.host} is chain {actual}, config is {self.config.chain_id}"
                )
            answered += 1
        if not answered:
            raise RpcUnavailableError("no RPC endpoint answered")
        logger.info("RPC endpoints: %s", ", ".join(endpoint.host for endpoint in self.endpoints))

    def block_number(self) -> int:
        return self.request(lambda endpoint: endpoint.web3.eth.block_number)

    def call(self, call: Call) -> bytes:
        return self.request(
            lambda endpoint: bytes(endpoint.web3.eth.call({"to": call.target, "data": call.data}))
        )

    def multicall(self, calls: list[Call]) -> list[CallResult]:
        results: list[CallResult] = []
        for batch in batched(calls, MULTICALL_BATCH):
            returned = self.call(self.multicall3.aggregate(list(batch)))
            results.extend(Multicall3.decode_aggregate(returned))
        return results


def describe(error: Exception) -> str:
    """An error in words that are safe to log. Only an RPC error's own message is kept."""
    if isinstance(error, Web3RPCError):
        return str(error)[:200]
    return type(error).__name__


def to_log(receipt: LogReceipt) -> Log:
    address: ChecksumAddress = receipt["address"]
    return Log(
        address=address,
        topics=tuple(bytes(t) for t in receipt["topics"]),
        data=bytes(receipt["data"]),
        block_number=receipt["blockNumber"],
    )
