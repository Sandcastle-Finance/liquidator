"""Simulates a liquidation at the latest block and, when armed, signs and sends it."""

import logging
from dataclasses import dataclass

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from web3.exceptions import Web3Exception
from web3.types import StateOverride, TxParams, Wei

from sandcastle_liquidator.chain import Chain, RpcUnavailableError
from sandcastle_liquidator.contracts import Call, Erc20
from sandcastle_liquidator.models import MAX_UINT256

logger = logging.getLogger(__name__)

# Stands in for the liquidator in simulations when no key is configured.
SIMULATION_ADDRESS = to_checksum_address("0x00000000000000000000000000000000000051A7")
GAS_HEADROOM_PERCENT = 130


@dataclass(frozen=True)
class Simulation:
    profit: int


@dataclass(frozen=True)
class Reverted:
    reason: str


class Executor:
    def __init__(self, chain: Chain, private_key: str) -> None:
        self.chain = chain
        self.account: LocalAccount | None = (
            Account.from_key(private_key) if private_key else None  # pyright: ignore[reportUnknownMemberType]
        )
        self.multicall_code = chain.request(
            lambda endpoint: bytes(endpoint.web3.eth.get_code(chain.config.multicall3))
        )

    @property
    def liquidator(self) -> ChecksumAddress:
        return self.account.address if self.account else SIMULATION_ADDRESS

    def simulate(
        self, bundle: Call, loan_token: ChecksumAddress, spenders: list[ChecksumAddress]
    ) -> Simulation | Reverted:
        """Runs the bundle as the liquidator and measures its loan-token balance change.

        For the length of the call the liquidator's address runs Multicall3's code, so one
        `eth_call` can grant the approvals, read the balance, run the bundle and read the
        balance again, all as the liquidator. Nothing here touches the chain.
        """
        balance = Erc20.balance_of(loan_token, self.liquidator)
        steps = [
            *[Erc20.approve(loan_token, spender, MAX_UINT256) for spender in spenders],
            balance,
            bundle,
            balance,
        ]
        outer = self.chain.multicall3.aggregate(steps)
        override: StateOverride = {self.liquidator: {"code": self.multicall_code}}
        try:
            returned = self.chain.request(
                lambda endpoint: endpoint.web3.eth.call(
                    {"from": self.liquidator, "to": self.liquidator, "data": outer.data},
                    "latest",
                    override,
                )
            )
        except (Web3Exception, RpcUnavailableError) as error:
            return Reverted(str(error))
        results = self.chain.multicall3.decode_aggregate(bytes(returned))
        before, ran, after = results[-3:]
        if not ran.success:
            return Reverted("0x" + ran.data.hex())
        return Simulation(Erc20.decode_amount(after.data) - Erc20.decode_amount(before.data))

    def ensure_allowance(self, token: ChecksumAddress, spender: ChecksumAddress) -> None:
        """Approve `spender` once, without limit. The core and GeneralAdapter1 only ever pull
        from the account that started the call, so the approval is usable by this EOA alone."""
        allowance = Erc20.decode_amount(
            self.chain.call(Erc20.allowance(token, self.liquidator, spender))
        )
        if allowance < MAX_UINT256 // 2:
            logger.info("approving %s to spend %s", spender, token)
            self.send(Erc20.approve(token, spender, MAX_UINT256))

    def send(self, call: Call) -> str:
        """Sign, send and wait for the receipt. Returns the transaction hash."""
        if self.account is None:
            raise MissingKeyError("PRIVATE_KEY is required to send transactions")
        sender = self.account.address
        request = self.chain.request
        transaction: TxParams = {
            "from": sender,
            "to": call.target,
            "data": call.data,
            "chainId": self.chain.config.chain_id,
            "nonce": request(lambda endpoint: endpoint.web3.eth.get_transaction_count(sender)),
            "maxFeePerGas": Wei(request(lambda endpoint: endpoint.web3.eth.gas_price) * 2),
            "maxPriorityFeePerGas": Wei(0),
        }
        estimate = request(lambda endpoint: endpoint.web3.eth.estimate_gas(transaction))
        transaction["gas"] = estimate * GAS_HEADROOM_PERCENT // 100
        signed = self.account.sign_transaction(transaction)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        # The same signed bytes go to each endpoint in turn until one accepts them, so a
        # second endpoint can only ever relay the same transaction.
        sent = request(
            lambda endpoint: endpoint.web3.eth.send_raw_transaction(signed.raw_transaction)
        )
        receipt = request(lambda endpoint: endpoint.web3.eth.wait_for_transaction_receipt(sent))
        if receipt["status"] != 1:
            raise TransactionFailedError(receipt["transactionHash"].hex())
        return receipt["transactionHash"].hex()


class MissingKeyError(Exception):
    pass


class TransactionFailedError(Exception):
    pass
