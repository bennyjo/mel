# Mel the self-improving yield allocator

[![CI](https://github.com/bennyjo/mel/actions/workflows/ci.yml/badge.svg)](https://github.com/bennyjo/mel/actions/workflows/ci.yml)

<img src=".github/mel.png" alt="Mel, a honeybee flying over a honeycomb under a waggle-dance path" width="100" align="left"/>

**Mel is a self-improving capital allocator: an AI agent that moves a
stablecoin bankroll between DeFi venues on Robinhood Chain and rewrites its
own allocation rules after every settled position.** Paper allocation is the
learning engine. Real capital runs alongside it only when the operator turns
it on, capped small, signed through
[Pearl Connect](https://www.pearl.you/connect) so the agent never
holds a key.

The name is short for Melissa, Greek for honeybee, and Latin for honey. A
forager bee measures a flower patch by net return: nectar gained minus the
energy spent flying there. Back at the hive it dances with intensity
proportional to that net return, and the colony moves foragers to the
patches that actually paid, not the ones with the brightest petals. Mel
does that with yield.

## The experiment

Every DeFi venue advertises a yield. None of them settle it. The number on
the dashboard is gross, instantaneous, and quoted before gas, swap cost,
rate decay, and impermanent loss. Mel gives Claude Code a simulated 1,000
USDG bankroll and the venues deployed on Robinhood Chain (chain ID 4663),
such as Uniswap pools and Morpho markets. Each cycle it:

1. settles every matured allocation against on-chain state read at that
   moment, and records what the position actually netted;
2. scores three numbers against each other: what the venue advertised, what
   the agent forecast, and what the position realized;
3. writes a retrospective when positions have settled;
4. edits its own playbook, risk policy, venue filters, and cadence, and
   commits the diff;
5. forecasts the net return of each candidate allocation, then allocates,
   or leaves the capital idle when nothing clears the cost of moving.

The strategy's git log is the product: every commit is a lesson the agent
paid for, in paper. The honest metric is `net_delta`, not profit. Does the
agent's forecast beat the advertised number and a do-nothing baseline, after
costs?

This public repository is a single-commit export of a private working
history. Short commit SHAs cited in the journal therefore don't resolve here,
and the retros in `journal/` record each strategy change. From the export on,
the git log is the product again.

Two layers keep it honest:

- an **hourly cycle agent** that settles, scores, self-edits, forecasts, and
  allocates;
- the **human operator**, who owns the protected engine (`core/`, the caps,
  the cycle procedure). Evidence flows in through
  `journal/operator-notes.md`. Asks flow out through `journal/proposals.md`.

## Real execution through Pearl Connect

Real allocation is dormant. `real_execution_enabled` is `false` in
`config/protected.json`, no venue is allowed for real use, and `loop.sh`
refuses to cycle if `python3 core/real.py doctor` ever reports ready. When
the operator turns it on, `core/real.py` is the only code that touches
funds. Its per-allocation, daily, and open-position caps live in
`config/protected.json`, with hard ceilings that CI enforces.

[Pearl Connect](https://www.pearl.you/connect) is Pearl's BYOA signing
service. It lets any agent harness, Claude Code included, act as a Pearl
agent without ever holding keys. Pearl Connect runs on Robinhood Chain: the
operator's Connect service is service #12 on the chain's service registry,
and `CHAIN.md` records its service safe. To run it yourself, download Pearl
at [pearl.you/connect](https://www.pearl.you/connect).

## Honest-simulation rules

- Entries and exits are priced at live on-chain state, including the swap's
  own price impact against real pool liquidity.
- Real gas is charged at the block's gas price, converted to USDG.
- An advertised or quoted APY is never recorded as a realized outcome.
- The forecast is written before the allocation, never after.
- Idle capital is a legal allocation and is scored like any other.
- A position must be exitable. The engine refuses an entry or a settlement
  when a Morpho market lacks free liquidity or a vault's simulated withdraw
  reverts.
- The agent cannot edit the engine (`core/`, `config/protected.json`).
  `loop.sh` reverts any attempt, and CI independently fails any agent
  commit that touches protected files.

## Repository layout

The operator owns the engine, and the agent owns its strategy and its
record. The following table lists who owns each path:

| Path | What it holds | Owner |
|---|---|---|
| `core/` | The engine: the RPC client (`rpc.py`), venue discovery and quoting (`venues.py`), the cost model (`cost.py`), the ledger (`ledger.py`), settlement (`settle.py`), the stake-free forecast ledger (`forecast.py`), scoring (`score.py`), the forecast-rule replay (`replay.py`), the runner lease (`lease.py`), real execution (`real.py`), integrity tripwires (`validate.py`), and offline tests (`tests/`) | Operator |
| `config/protected.json` | Bankroll, allocation and venue caps, the default RPC endpoint, and the real-execution caps | Operator |
| `CYCLE.md` | The procedure the agent follows each cycle | Operator |
| `CHAIN.md` | Facts about chain 4663, each verified against the chain | Operator |
| `loop.sh` | Runs cycles headlessly with Claude Code and reverts agent edits to operator-owned paths | Operator |
| `.github/` | CI, the boundary guard, and community files | Operator |
| `strategy/` | Playbook, forecast rule, risk policy, venue filters, and the full-cycle schedule | Agent |
| `journal/` | Allocation and forecast ledgers, venue record, retrospectives, cycle log, RPC usage, operator notes, and proposals | Agent, with ledger rows written only through `core/` and operator notes by the operator |

## Quickstart

The engine is Python 3 standard library only, so there is nothing to
install. CI runs on Python 3.12. These commands work offline on a fresh
clone:

```bash
git clone https://github.com/bennyjo/mel.git
cd mel
python3 -m unittest discover -s core/tests -t .   # offline tests
python3 core/validate.py                          # integrity tripwires
python3 core/score.py                             # net_delta and calibration report
```

To check that you can reach the chain, run `python3 core/rpc.py`. It prints
the endpoint, the chain ID, the head block, and the gas price. Every command
that reads the chain appends its request count to `journal/rpc-usage.jsonl`.

## Run cycles

A cycle is a headless [Claude Code](https://claude.com/claude-code) session
that follows `CYCLE.md`. `loop.sh` runs cycles one after another:

```bash
./loop.sh 10 60          # 10 paper cycles, 60 minutes apart
python3 core/score.py    # net_delta and calibration report, any time
```

Before you run `loop.sh`, know what it does:

- It needs `claude` on your `PATH`, and each cycle uses your Claude Code
  account. `loop.sh` pins the model per cycle: `claude-opus-5-5` for the
  four full-cycle hours a day in `strategy/schedule.json` and for repair
  cycles, and `claude-sonnet-5-5` for the light ticks in between. It runs the
  session with `--permission-mode acceptEdits` and a fixed tool allowlist:
  the file tools, `python3 core/*`, and the git commands a cycle needs to
  commit. The allowlist has no web search, no web fetch, and no `git push`.
- Each cycle commits, and `loop.sh` pushes to `main` on your `origin`
  remote. Before each cycle it takes the runner lease (`core/lease.py`),
  which is also a push to `origin`. On a clone that can't push to `origin`,
  such as a clone without write access, every tick is skipped with
  `WARNING: runner lease not taken`. To run your own Mel, fork the
  repository and clone your fork. To run locally without pushing anywhere,
  remove the remote with `git remote remove origin`; without an `origin`
  remote, the lease and the push are both skipped.
- A fork starts from Mel's state on the day you fork it: the ledgers, open
  allocations, strategy, and journal.
- Cycles read the chain through the public Robinhood Chain RPC unless you
  set `MEL_RPC_URL`. The public endpoint rate-limits hard and serves only
  about ten minutes of state. For reliable runs, use your own endpoint. For
  more information, see [Costs](#costs).
- Cycles stay on paper. `loop.sh` has no real-money mode, and it checks
  before every cycle that real execution is not ready.

In Mel's own deployment, the hourly cycles run as scheduled cloud routines
that the operator configures outside this repository, with `MEL_RPC_URL` set
to the operator's endpoint. `loop.sh` is the runner that ships with it.

## Configuration

Caps and engine settings live in `config/protected.json`; the agent's sizing
policy lives in `strategy/risk.json`, inside those caps. All environment
variables are optional:

| Variable | Read by | Purpose |
|---|---|---|
| `MEL_RPC_URL` | `core/rpc.py` | JSON-RPC endpoint for chain 4663. Overrides `rpc_url` in `config/protected.json`, which is the public Robinhood Chain RPC. |
| `MEL_CONTEXT` | `core/rpc.py` | Label written with each run's request count in `journal/rpc-usage.jsonl`. Defaults to `adhoc`. |
| `MEL_RUNNER` | `core/lease.py` | Runner name for the lease. Defaults to `operator` under `loop.sh`, else `cloud`, followed by a random ID kept in the checkout. |
| `PEARL_CONNECT_STORE` | `core/real.py` | Pearl Connect workspace directory (contains `.mcp.json`). Only the operator's real-execution path uses it. |
| `MEL_LEASE`, `MEL_PUSH_BY_LOOP` | `CYCLE.md`, `core/lease.py` | Set by `loop.sh` to tell the cycle that it already holds the lease and pushes for the session. Don't set them yourself. |

Keep keys and tokens outside the repository. The journal and strategy are
public, and every cycle commits them.

## Costs

Mel runs on paper, so it spends no gas and no capital. Running it costs
Claude Code usage and RPC requests. The following figures are from one
measured day, 2026-10-21 UTC, of the operator's cloud routines:

| Item | Measured |
|---|---|
| Full cycles (`claude-opus-5-5`) | 4 sessions, 73 minutes in total, 3 to 30 minutes each |
| Light ticks (`claude-sonnet-5-5`) | 20 sessions, 100 minutes in total, a median under 3 minutes each |
| RPC | 211,666 JSON-RPC calls in 9,271 HTTP requests |

Cloud routine logs don't report token counts, so the Claude figures are
session time rather than tokens. What a day costs you depends on your Claude
plan or API pricing.

RPC volume ranged from 188,000 to 344,000 calls a day between 2026-10-14
and 2026-10-22, from `journal/rpc-usage.jsonl`. `core/rpc.py` batches
calls and backs off when the endpoint throttles, so the public endpoint
works, but a full cycle on it runs slower. Before the operator moved to a
dedicated endpoint, throttling and refused `eth_getLogs` queries on the
public endpoint voided about a third of the forecast ledger. For reliable
runs, set `MEL_RPC_URL` to an endpoint you pay for.

## Results

Results as of 2026-11-03, cycle 1159, from `python3 core/score.py`. Run it for
the current numbers.

- **Paper result:** +33.15 USDG on the 1,000 USDG bankroll since the first
  allocation on 2026-09-09, after 1.23 USDG of simulated gas and swap
  costs. Of 72 settled allocations, 31 were deployed and 41 stayed idle;
  3 more were voided and 3 are open.
- **No settled position lost money** (the smallest made +0.29 USDG). One was
  marked as a total loss for about a day before it settled; see the last
  item in this list. Rates fell through late October, but every deployed row
  still realized at least 9.0% net APY, so Mel hasn't faced rates too low to
  cover its costs.
- **Forecast error halved.** The mean absolute `net_delta` of deployed
  allocations fell from 35.4 percentage points (the first 15 settled,
  2026-09-17 to 2026-10-10) to 13.2 (the last 16, 2026-10-13 to
  2026-10-31). Rates also fell over that period, which shrinks errors in
  points: the mean realized rate went from 77.3% to 59.9%. Realized returns beat the forecast in 24 of 31
  deployed rows, so Mel still forecasts low. (`core/score.py` prints 65/72
  because it counts the 41 idle rows, which tie.) Mel's 22 strategy commits
  show how: it learned to anchor on trailing rates instead of shading below
  them.
- **No skill shown against a baseline.** On the stake-free forecast ledger
  (1,329 settled forecasts), the agent's mean absolute error is 1.97 points
  lower than the best mechanical baseline for each venue kind and horizon,
  with a 95% interval of -4.45 to +0.51. The interval includes zero, so the
  score can't tell the agent from a baseline that uses the trailing 24-hour,
  trailing 7-day, or advertised rate.
- **An exit the chain wouldn't give.** One vault twice ran out of idle USDG
  in October, so every withdraw reverted. The engine kept `a00036` open until
  a simulated withdraw succeeded, and it settled 4.6 days past its horizon.
  `a00065` stuck the same way, 4.5 days past its horizon. By then, a position
  that can't exit 24 hours past its horizon is marked at what the venue can
  pay, so `a00065` was booked at -100% (-250.03 USDG) for about 26 hours. It
  settled at +1.91 USDG when the vault paid it back. The agent has since
  blocked that vault.

The score also reports annualized rates, such as +68.34% realized on
deployed allocations. Those are 250 USDG positions held for 1 to 7 days in
thin venues. Don't read them as a yield anyone can expect.

## Status

Mel is a running experiment, not a versioned release. The agent commits to
`main` many times a day, and `main` is the only supported version. A daily
deep-retro agent that audits every strategy edit is planned and isn't
built.

## Disclaimer

This is a research experiment in agent self-improvement. Allocation is
simulated unless the operator deliberately enables the real leg, which is
capped in `config/protected.json` and signed through Pearl Connect. Nothing
here is financial or investment advice. Past performance, paper or real,
predicts nothing.

Venue observations in the journal, such as rates, liquidity, and failed
withdrawals, are automated readings of on-chain state. They aren't ratings
or recommendations of any protocol.

Mel isn't affiliated with or endorsed by Robinhood.

## Contributing and security

- [Contributing guide](.github/CONTRIBUTING.md): what is accepted, and the
  checks to run before a pull request.
- [Code of conduct](.github/CODE_OF_CONDUCT.md)
- [Security policy](.github/SECURITY.md): report anything that touches funds,
  keys, or the protected boundary privately.

## License

[Apache-2.0](LICENSE). The journal and strategy files are part of the
experiment's record and are covered by the same license.
