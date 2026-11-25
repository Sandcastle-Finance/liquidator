from pathlib import Path

import pytest
from eth_utils.address import to_checksum_address
from requests.exceptions import ConnectionError as RequestsConnectionError
from web3.exceptions import ContractLogicError, Web3RPCError

from sandcastle_liquidator.chain import Chain, Endpoint, RpcUnavailableError
from sandcastle_liquidator.config import CONFIG_DIR, expand_env, load_chain, load_rpcs
from sandcastle_liquidator.models import Log, LogQuery, RpcEndpoint
from sandcastle_liquidator.sources.rpc import RpcLogSource

ADDRESS = to_checksum_address("0x" + "11" * 20)
OTHER = to_checksum_address("0x" + "22" * 20)
TOPIC = b"\x01" * 32


class FakeEndpoint(Endpoint):
    """Answers `eth_getLogs` with one log per request, refusing spans over its limits."""

    def __init__(self, host: str, single: int, multi: int, down: bool = False) -> None:
        super().__init__(RpcEndpoint(f"https://{host}/?key=SECRET", single, multi))
        self.down = down
        self.spans: list[int] = []

    def get_logs(self, query: LogQuery) -> list[Log]:
        if self.down:
            raise RequestsConnectionError(f"cannot reach {self.config.url}")
        span = query.to_block - query.from_block + 1
        limit = (
            self.config.max_log_range
            if len(query.addresses) == 1
            else self.config.max_log_range_multi
        )
        if span > limit:
            raise Web3RPCError("block range exceeds maximum allowed")
        self.spans.append(span)
        return [Log(query.addresses[0], (TOPIC,), b"", query.from_block)]


def chain_with(*endpoints: Endpoint) -> Chain:
    chain = Chain(load_chain(CONFIG_DIR / "robinhood.toml"))
    chain.endpoints = list(endpoints)
    return chain


def test_logs_are_split_to_the_answering_endpoints_spans() -> None:
    public = FakeEndpoint("public.example", 1_000_000, 100_000, down=True)
    narrow = FakeEndpoint("narrow.example", 10_000, 10_000)
    source = RpcLogSource(chain_with(public, narrow))

    logs = source.get_logs(LogQuery(TOPIC, 1, 25_000, (ADDRESS,)))

    assert narrow.spans == [10_000, 10_000, 5_000]
    assert [log.block_number for log in logs] == [1, 10_001, 20_001]


def test_the_first_endpoint_is_used_while_it_answers() -> None:
    public = FakeEndpoint("public.example", 1_000_000, 100_000)
    narrow = FakeEndpoint("narrow.example", 10_000, 10_000)
    source = RpcLogSource(chain_with(public, narrow))

    source.get_logs(LogQuery(TOPIC, 1, 250_000, (ADDRESS, OTHER)))

    # Two addresses over 250,000 blocks: one address at a time takes two requests, both
    # together in 100,000-block spans would take three.
    assert public.spans == [250_000, 250_000]
    assert narrow.spans == []


def test_a_failed_endpoint_is_passed_over_until_its_cooldown_ends() -> None:
    public = FakeEndpoint("public.example", 1_000_000, 100_000, down=True)
    narrow = FakeEndpoint("narrow.example", 10_000, 10_000)
    chain = chain_with(public, narrow)
    asked: list[str] = []

    def ask(endpoint: Endpoint) -> list[Log]:
        asked.append(endpoint.host)
        return endpoint.get_logs(LogQuery(TOPIC, 1, 1, (ADDRESS,)))

    chain.request(ask)
    chain.request(ask)
    public.retry_at = 0.0
    chain.request(ask)

    assert asked == [
        "public.example",
        "narrow.example",
        "narrow.example",
        "public.example",
        "narrow.example",
    ]


def test_when_every_endpoint_fails_the_error_names_hosts_and_no_keys() -> None:
    chain = chain_with(
        FakeEndpoint("public.example", 1, 1, down=True),
        FakeEndpoint("narrow.example", 1, 1, down=True),
    )

    with pytest.raises(RpcUnavailableError) as raised:
        chain.request(lambda endpoint: endpoint.get_logs(LogQuery(TOPIC, 1, 1, (ADDRESS,))))

    assert "public.example" in str(raised.value)
    assert "narrow.example" in str(raised.value)
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_a_revert_is_the_answer_and_no_other_endpoint_is_asked() -> None:
    first = FakeEndpoint("public.example", 1, 1)
    second = FakeEndpoint("narrow.example", 1, 1)
    asked: list[str] = []

    def revert(endpoint: Endpoint) -> None:
        asked.append(endpoint.host)
        raise ContractLogicError("execution reverted")

    with pytest.raises(ContractLogicError):
        chain_with(first, second).request(revert)

    assert asked == ["public.example"]


def test_an_endpoint_whose_key_is_not_set_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = [
        {"url": "https://public.example", "max_log_range": 5, "max_log_range_multi": 4},
        {
            "url": "https://keyed.example/${TEST_RPC_KEY}",
            "max_log_range": 3,
            "max_log_range_multi": 2,
        },
    ]
    monkeypatch.delenv("TEST_RPC_KEY", raising=False)
    assert [rpc.url for rpc in load_rpcs(raw)] == ["https://public.example"]

    monkeypatch.setenv("TEST_RPC_KEY", "abc")
    assert load_rpcs(raw)[1] == RpcEndpoint("https://keyed.example/abc", 3, 2)
    assert expand_env("no variables") == "no variables"


def test_the_robinhood_config_lists_the_public_endpoint_first() -> None:
    chain = load_chain(Path(CONFIG_DIR / "robinhood.toml"))
    assert chain.rpcs[0].url == "https://rpc.mainnet.chain.robinhood.com"
