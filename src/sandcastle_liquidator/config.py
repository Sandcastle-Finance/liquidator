"""Settings from the environment and the per-chain TOML file."""

import os
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from sandcastle_liquidator.models import ChainConfig, Factory, KyberSwapConfig

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class LogSourceKind(StrEnum):
    RPC = "rpc"
    HYPERSYNC = "hypersync"


@dataclass(frozen=True)
class Settings:
    chain: ChainConfig
    rpc_url: str
    log_source: LogSourceKind
    envio_api_token: str
    poll_seconds: float
    private_key: str
    armed: bool
    max_slippage_bps: int
    min_profit_bps: int
    kyberswap_client_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        chain = load_chain(CONFIG_DIR / f"{os.environ.get('CHAIN', 'robinhood')}.toml")
        return cls(
            chain=chain,
            rpc_url=os.environ.get("RPC_URL") or chain.rpc_url,
            log_source=LogSourceKind(os.environ.get("LOG_SOURCE") or "rpc"),
            envio_api_token=os.environ.get("ENVIO_API_TOKEN", ""),
            poll_seconds=float(os.environ.get("POLL_SECONDS") or 10),
            private_key=os.environ.get("PRIVATE_KEY", ""),
            armed=os.environ.get("ARM") == "1",
            max_slippage_bps=int(os.environ.get("MAX_SLIPPAGE_BPS") or 100),
            min_profit_bps=int(os.environ.get("MIN_PROFIT_BPS") or 10),
            kyberswap_client_id=os.environ.get("KYBERSWAP_CLIENT_ID")
            or "sandcastle-liquidation-bot",
        )


def load_chain(path: Path) -> ChainConfig:
    with path.open("rb") as file:
        raw: dict[str, Any] = tomllib.load(file)
    kyberswap: dict[str, Any] | None = raw.get("kyberswap")
    return ChainConfig(
        chain_id=int(raw["chain_id"]),
        name=str(raw["name"]),
        rpc_url=str(raw["rpc_url"]),
        hypersync_url=str(raw["hypersync_url"]),
        max_log_range=int(raw["max_log_range"]),
        max_log_range_multi=int(raw["max_log_range_multi"]),
        morpho=to_checksum_address(raw["morpho"]),
        bundler3=to_checksum_address(raw["bundler3"]),
        general_adapter1=to_checksum_address(raw["general_adapter1"]),
        swap_adapter=to_checksum_address(raw["swap_adapter"]),
        universal_router=optional_address(raw.get("universal_router")),
        slipstream_router=optional_address(raw.get("slipstream_router")),
        multicall3=to_checksum_address(raw["multicall3"]),
        wrapped_native=to_checksum_address(raw["wrapped_native"]),
        kyberswap=KyberSwapConfig(str(kyberswap["chain"]), to_checksum_address(kyberswap["router"]))
        if kyberswap
        else None,
        factories=tuple(
            Factory(to_checksum_address(f["address"]), int(f["start_block"]))
            for f in raw["factories"]
        ),
    )


def optional_address(value: str | None) -> ChecksumAddress | None:
    return to_checksum_address(value) if value else None
