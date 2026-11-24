"""Swap calldata from the KyberSwap aggregator API."""

from dataclasses import dataclass
from typing import Any

import requests
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from sandcastle_liquidator.models import KyberSwapConfig

API = "https://aggregator-api.kyberswap.com"
TIMEOUT_SECONDS = 10


class KyberSwapError(Exception):
    pass


@dataclass(frozen=True)
class AggregatorSwap:
    """A swap of exactly `amount_in`, executed by calling `router` with `data` after
    approving it. `amount_out` is the quote; the calldata enforces the slippage bound."""

    router: ChecksumAddress
    token_in: ChecksumAddress
    amount_in: int
    amount_out: int
    data: bytes


class KyberSwap:
    def __init__(self, config: KyberSwapConfig, client_id: str) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers["x-client-id"] = client_id

    def swap(
        self,
        token_in: ChecksumAddress,
        token_out: ChecksumAddress,
        amount_in: int,
        sender: ChecksumAddress,
        recipient: ChecksumAddress,
        slippage_bps: int,
        deadline: int,
    ) -> AggregatorSwap:
        """Find a route and build its transaction: `sender` pays, `recipient` is paid."""
        route = self.request(
            "GET",
            "routes",
            params={"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount_in)},
        )
        built = self.request(
            "POST",
            "route/build",
            json={
                "routeSummary": route["routeSummary"],
                "sender": sender,
                "recipient": recipient,
                "slippageTolerance": slippage_bps,
                "deadline": deadline,
            },
        )
        router = to_checksum_address(built["routerAddress"])
        if router != self.config.router:
            raise KyberSwapError(f"unexpected router {router}, config has {self.config.router}")
        if int(built["amountIn"]) != amount_in or int(built["transactionValue"]) != 0:
            raise KyberSwapError("built swap does not match the request")
        return AggregatorSwap(
            router=router,
            token_in=token_in,
            amount_in=amount_in,
            amount_out=int(built["amountOut"]),
            data=bytes.fromhex(str(built["data"]).removeprefix("0x")),
        )

    def request(self, method: str, path: str, **arguments: Any) -> dict[str, Any]:  # noqa: ANN401
        url = f"{API}/{self.config.chain}/api/v1/{path}"
        try:
            response = self.session.request(method, url, timeout=TIMEOUT_SECONDS, **arguments)
            body: dict[str, Any] = response.json()
        except (requests.RequestException, ValueError) as error:
            raise KyberSwapError(f"{path}: {error}") from error
        data: dict[str, Any] | None = body.get("data")
        if body.get("code") != 0 or data is None:
            raise KyberSwapError(f"{path}: {body.get('message') or response.status_code}")
        return data
