# Sandcastle liquidator

A small bot that finds unhealthy [Sandcastle](https://app.sandcastle.finance) positions and
liquidates them with a Morpho flash loan, run from a plain EOA. No contract to deploy and no
capital needed beyond gas.

**Status: experimental.** Discovery and health checks run against the live chain, and the
full liquidation has been sent successfully on a local fork. It is a dry run unless `ARM=1`.

## How it works

1. **Discover.** `SetupDeployed` on each FactoryFactory gives the Sandcastle cores and their
   Morpho markets; `PositionRegistered` on each core gives the borrowers. Logs come from the RPC
   (default) or from Envio HyperSync.
2. **Check health.** Each poll reads `morpho.position`, `morpho.market` and
   `core.viewPrice(borrower, loanToken)` through Multicall3. A position is liquidatable when its
   debt is above `collateral * price / 1e36 * LLTV`.
3. **Liquidate.** One `Bundler3.multicall` from the EOA, with no custom contract:
   flash-borrow the loan token from Morpho, call `core.liquidate`, sell the redeemed pool
   tokens for the loan token, repay the flash loan and keep the rest.

The core pulls the repayment from the liquidator, so the EOA approves the loan token to the
core and to GeneralAdapter1 the first time it liquidates on a market. Both only ever pull from
the account that started the call.

### Where the swap goes

Each liquidation is built once per route, simulated at the latest block, and sent on the
route with the higher simulated profit, provided that is at least `MIN_PROFIT_BPS` of the
flash loan.

- **KyberSwap.** The redeemed amounts are estimated from the position's liquidity at the
  pool's current price, and each token that is not the loan token is quoted on the
  [KyberSwap aggregator API](https://docs.kyberswap.com/kyberswap-solutions/kyberswap-aggregator).
  Inside the bundle, Bundler3 takes exactly the quoted input, approves KyberSwap's router for
  it and calls the router with the API's calldata; the output goes to the EOA. This also
  covers markets whose loan token is not in the pool. The bot only calls the router address
  pinned in the chain config.
- **Same pool.** The protocol's SwapAdapter sells the pool's other token back through the
  position's own pool. No API involved, but the liquidation has just removed liquidity from
  that pool, and it only works when the loan token is one of the pool's two tokens.

## Limits

- Liquidations are full: all the debt, or all the collateral when the position is underwater.
- `MAX_SLIPPAGE_BPS` is measured against the KyberSwap quote, or against the pool's price at
  simulation time. That bounds the swap, not the profit: a market moved between simulation
  and inclusion can still cost up to that slippage, and nothing on-chain checks the profit.
- The KyberSwap route sells slightly less than the estimate, so a little of the seized token
  (and any uncollected fees) reaches the EOA unsold and is not counted as profit.
- The same-pool route skips Metric pools and AMMs with no router in the chain config.
- Profit is measured in the loan token and does not subtract gas.

## Running it

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

**1. Install.**

```sh
git clone https://github.com/Sandcastle-Finance/liquidator.git
cd liquidator
uv sync                      # add --extra hypersync to read logs from HyperSync
```

**2. Look at the market.** No key or configuration needed; this prints every position with
debt, least healthy first.

```sh
uv run sandcastle-liquidator status
```

**3. Configure.** Settings are environment variables, documented in `.env.example`. The bot
does not read `.env` by itself, so load it into your shell:

```sh
cp .env.example .env         # edit it, then:
set -a; source .env; set +a
```

| Variable | What it does |
|---|---|
| `PRIVATE_KEY` | The liquidating EOA. Without it the bot only simulates. |
| `ARM` | `1` sends transactions. Anything else is a dry run. |
| `RPC_URL` | Overrides the public RPC. A private endpoint is strongly recommended. |
| `MAX_SLIPPAGE_BPS` | Allowed swap slippage, default 100 (1%). |
| `MIN_PROFIT_BPS` | Minimum simulated profit as a share of the flash loan, default 10. |
| `LOG_SOURCE` | `rpc` (default) or `hypersync`, which also needs `ENVIO_API_TOKEN`. |
| `POLL_SECONDS` | Seconds between health checks, default 10. |

**4. Dry run.** With `PRIVATE_KEY` set and `ARM` unset, the bot finds liquidations, simulates
them as your account and logs the profit, but sends nothing.

```sh
uv run sandcastle-liquidator run
```

**5. Go live.** Fund the EOA with native gas (it needs nothing else; repayments come from the
flash loan), set `ARM=1` and run the same command. The first liquidation on a market also
sends two unlimited approvals of the loan token, to the core and to GeneralAdapter1. Profit
accumulates in the EOA in each market's loan token.

Use a dedicated key that holds only gas. Run one instance per key: two would race each other
on the same nonce.

### With Docker

```sh
docker build -t sandcastle-liquidator .
docker run --rm --env-file .env sandcastle-liquidator
```

### Other chains

Chains are files in `config/`. Add one with that deployment's addresses and set `CHAIN` to
its name.

## Deploying to Fly.io

`fly.toml` runs the bot as one always-on machine with no public service and no volume.

```sh
fly launch --copy-config --no-deploy      # once, to create the app under your account
fly secrets set PRIVATE_KEY=0x...         # the liquidating EOA; add RPC_URL for a private RPC
fly deploy                                # starts as a dry run
fly logs                                  # check the liquidator address and the scans
fly secrets set ARM=1                     # go live
```

The EOA needs native gas, and nothing else: repayments come from the flash loan. Keep one
machine only (`fly scale count 1`); two would race each other with the same nonce.

## Development

```sh
uv run ruff format . && uv run ruff check .
uv run pyright
uv run pytest
```

## Risk

Liquidation bots lose money to gas, slippage and faster competitors. Health here uses Morpho's
last accrued totals, so debt is slightly understated until the market next accrues interest.
This software is provided as is, with no warranty; read the code before running it with funds.
