# Forecast ledger: rate skill without a stake, 2026-09-24

Operator-owned. This memo records what the forecast ledger is, the first
rows it wrote against chain 4663, how fast it should produce evidence, and
what building it showed about the chain.

Mel learns only from settled allocations, about two deployed rows a week.
What Mel is learning, a forecast rate against a realized rate, needs no
capital. Phil records a forecast for every market it researches (1,076
forecasts against 48 bets), and that volume answered in weeks whether it
beats the market. `journal/forecasts.jsonl` does the same for rates.

## What was built

- `core/forecast.py` (protected) is the only writer of
  `journal/forecasts.jsonl`. Its docstring holds the rules. It has five
  commands: `record`, `record-baselines`, `due`, `settle`, and `status`.
- Two forecasters write rows. Core records `baseline` rows mechanically for
  every venue a quote touches, and for the next horizon from the exit read
  that settles a row. The cycle agent records `agent` rows for each venue it
  researched on a full cycle, at 1440, 4320, and 10080 minutes, before any
  allocation decision.
- Every row carries four baselines read at the same moment: the advertised
  spot rate, the venue's own trailing 24-hour and 7-day rate, and the
  advertised rate times `forecast_haircut` from `strategy/risk.json`. The
  trailing start price comes from the venue's own events (Morpho `Supply` and
  `Withdraw`, ERC-4626 `Deposit` and `Withdraw`), because the endpoint
  forgets state after ten minutes. A baseline that can't be read is null,
  and `baseline_notes` says why.
- One live row per (forecaster, venue, market_id, horizon). A new forecast
  on a live key is refused, never superseded.
- A row settles at the first tick after its horizon, through
  `settle.realized_gross` on a notional position of `max_allocation_usdg`,
  annualized over the actual hold. The exit is held to the free-liquidity
  and withdraw-simulation checks a real exit gets. A row that can't be
  settled honestly is void, with the reason.
- A venue whose own advertised or trailing rate at entry is outside
  (-100%, 1000%] gets no row. Building this found the reason: see "Chain
  facts".
- `core/score.py` has a forecast section, kept apart from the allocation
  score. For each forecaster, kind, and horizon it reports the MAE and bias
  of every predictor, and the agent's error against the best baseline with a
  cluster-robust interval. It also reports win rates, n, clusters, and
  effective n. Clusters are (kind, UTC day of entry). Until there are 10
  clusters and an effective n of 30, the verdict reads "too few to say".
- `core/validate.py` checks every row: the schema, one live row per key, no
  realized field while open, no forecast written more than 30 minutes after
  its entry block, no rate outside the forecast domain, and settled numbers
  that recompute exactly.
- `CYCLE.md` settles forecasts in step 1 on every tick, explains the score
  in step 2, records baselines through the step 5 quote, and records agent
  forecasts in step 6.
- `core/tests/test_forecast.py` and `core/tests/test_forecast_score.py`
  replay recorded chain state with the network forbidden.

## First rows

At 12:33 UTC the board quote (`--rank-by rate --top 20 --min-depth 25000`)
recorded 51 baseline rows for 17 venues at block 71,372,824. It skipped
three venues: a Uniswap v2 pair, which has no settlement maths, and the two
Arcus 3x Long vaults. The operator then recorded agent forecasts for three
venues with the notes on the rows, marked `cycle: operator-proof`. They're
the first nine `agent` rows and aren't the cycle agent's. A second forecast
on a live Steakhouse key was refused.

`python3 core/forecast.py status` after the proof:

```
60 forecast rows, 60 open, 0 settled, 0 void, 0 due now, next due 2026-09-25T12:33:44Z

forecaster kind                  open  settled  void  due
agent      erc4626_usdg_vault       6        0     0    0
agent      morpho_market            3        0     0    0
baseline   erc4626_usdg_vault      18        0     0    0
baseline   morpho_market           21        0     0    0
baseline   uniswap_v3_pool         12        0     0    0

17 venues hold a live row
```

The baselines each venue's rows carry, and the agent's forecasts at 1, 3,
and 7 days:

| Forecaster | Kind | Venue | Advertised | Trailing 24h | Trailing 7d | Haircut | Agent forecast |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| baseline | morpho_market | `0xaa586d26` | 17.26% | 40.92% | 36.01% | 10.35% | - |
| baseline | morpho_market | `0xaba3ac50` | 9.52% | - | 1.38% | 5.71% | - |
| baseline | morpho_market | `0x1efd13a2` | 4.99% | 4.97% | 4.98% | 2.99% | - |
| baseline | morpho_market | `0xc845da65` | 4.04% | 4.04% | 3.87% | 2.42% | - |
| baseline | morpho_market | `0x919a9b6b` | 3.60% | 3.59% | 3.83% | 2.16% | - |
| baseline | morpho_market | `0x0309c02d` | 2.73% | 2.73% | 3.10% | 1.64% | - |
| baseline | morpho_market | `0x7c820d6a` | 0.03% | - | 0.04% | 0.02% | - |
| baseline | erc4626_usdg_vault | `0xd42174d3` Flock Credit Vault | 70.61% | 117.36% | 102.61% | 52.96% | - |
| baseline | erc4626_usdg_vault | `0xf37c0c59` ArcadiaV2 Global Dollar Debt | 18.04% | 22.65% | 19.84% | 13.53% | - |
| baseline | erc4626_usdg_vault | `0xa5e1e1f9` Senior ArcadiaV2 Global Dollar | 13.23% | 14.25% | 14.14% | 9.92% | - |
| baseline | erc4626_usdg_vault | `0xbeeff033` Steakhouse USDG | 3.82% | 3.89% | 3.89% | 2.87% | - |
| baseline | erc4626_usdg_vault | `0xde770c84` Spark Savings USDG | 3.44% | 3.46% | 3.50% | 2.58% | - |
| baseline | erc4626_usdg_vault | `0x026df18f` | 0.02% | -0.01% | 0.02% | 0.01% | - |
| baseline | uniswap_v3_pool | `0x69bfaf19` | 56.36% | - | - | 22.54% | - |
| baseline | uniswap_v3_pool | `0x52e65b17` | 48.75% | - | - | 19.50% | - |
| baseline | uniswap_v3_pool | `0x9b050cb1` | 27.22% | - | - | 10.89% | - |
| baseline | uniswap_v3_pool | `0xa9188730` | 0.00% | - | - | 0.00% | - |
| agent | morpho_market | `0xaa586d26` | 17.26% | 40.89% | 36.01% | 10.35% | 28.00%, 28.00%, 27.00% |
| agent | erc4626_usdg_vault | `0xa5e1e1f9` Senior ArcadiaV2 Global Dollar | 13.23% | 14.27% | 14.15% | 9.92% | 14.00%, 14.00%, 13.80% |
| agent | erc4626_usdg_vault | `0xbeeff033` Steakhouse USDG | 3.82% | 3.92% | 3.89% | 2.87% | 3.88%, 3.87%, 3.85% |

The table already shows why the advertised rate is too easy a baseline. On
`0xaa586d26` it's 17% against a market that paid 41% over the day, and on
Flock it's 71% against 117%.

## Expected throughput

These estimates assume this board: 17 venues that pass the rules, with 7
Morpho markets, 6 vaults, and 4 v3 pools.

- **Baseline rows.** Settle records the next row from its own exit read, so
  each key runs back to back. The 1-day key settles about once every 24 to
  25 hours, the 3-day key every 3 days, and the 7-day key weekly. That's
  about 1.4 settled rows per venue per day, or about 25 a day for the board.
- **Agent rows.** These are recorded on full cycles only, which run every
  six hours, so a 1-day key frees and is taken again every 24 to 30 hours.
  At three venues a cycle that's about 4 settled agent rows a day. If the
  agent forecasts the 8 to 10 venues it writes up on a typical full cycle,
  it's 10 to 13 a day.
- **Against the allocation ledger.** Two deployed rows a week is 0.29 a
  day. The ledger settles 30 to 40 forecast rows a day, about 100 to 130
  times that rate. Agent rows alone are 15 to 45 times that rate.
- **Effective n grows much more slowly than n.** Rates are autocorrelated,
  and the markets share one interest-rate model, so the score clusters by
  (kind, UTC day). That gives at most 3 clusters a day for baseline rows and
  2 for agent rows, which rarely forecast v3 pools. Rows in a cluster move
  together, so effective n per day sits near the cluster count. The 1-day
  agent verdict needs 30 effective observations, so expect it in about two
  weeks. The 7-day horizon gets fresh entry days only weekly, so its own
  verdict takes a season. The allocation ledger would need 15 weeks for 30
  rows, each its own cluster. In independent information, the forecast
  ledger is about 7 times faster, not 100.

## Chain facts CHAIN.md does not record

Each fact was read from chain 4663 on 2026-09-24.

- `eth_getLogs` results carry a `blockTimestamp` field, and it's always
  `0x0` on the public endpoint. Read timestamps with `eth_getBlockByNumber`.
- The chain produces about 9.91 blocks a second, or about 856,000 blocks a
  day. Block 71,372,824 has the timestamp 2026-09-24 12:33:44 UTC.
- Historical `eth_getLogs` answers fast when the window is narrow. The seven
  USDG Morpho markets deeper than 25,000 USDG emit about 3,400 `Supply` and
  `Withdraw` events a day, about 90% of them on `0xc845da65`. Scans over 18
  hours and 5.25 days of blocks finished in seconds, even while the quote's
  6M-block vault rate scan was throttled. A trailing baseline can exist when
  the advertised rate doesn't: Steakhouse USDG once quoted with no
  advertised rate but both trailing rates.
- Arcus BTC (3x Long), `0x4472c69d`, and Arcus HOOD (3x Long),
  `0xe24cabdf`, are ERC-4626 vaults with USDG as the asset. Their share
  price follows a leveraged BTC or HOOD position. Annualized, their
  trailing week reads +17,082,740% and +15,244,729%, and HOOD's trailing
  day reads -100.00%. The advertised 3,054% and 1,365% that `core/venues.py`
  infers from their events are price moves, not yield. One day of such a row
  would outweigh every lending row in a mean error, which is why the ledger
  refuses any venue with a rate over 1000% at entry.
- Morpho `0xaa586d26` read 40.58% at 12:27 UTC and 17.26% at 12:33 UTC. It
  sits at 88.0% utilization against the AdaptiveCurveIRM's 90% target, with
  a rate at target of 18.2% APR. Supply pays 17% there and 55% at 95%
  utilization. It paid 40.9% over the prior day, so its spot rate is one
  side of a utilization that crosses the kink during the day. The deep
  markets sit at 89% to 91%, where the rate at target drifts about 0.4% of
  itself a day.
- The same `--top 20` board quote took 3 minutes 5 seconds at 12:27 UTC and
  49 seconds at 12:33 UTC. Throttling varies that much within minutes.

## Known limits

- The domain rule sees only entry-time numbers. A leveraged vault whose
  price fell over both trailing windows, and whose inferred advertised rate
  stayed under 1000%, would get a row. A rule that also judged the realized
  rate would drop rows for their outcome, which is worse.
- ArcadiaV2 Global Dollar Debt (`0xf37c0c59`) still reverts `withdraw` with
  `0xced4f633` for every depositor. The board quote doesn't simulate a
  withdraw, so the quote gets baseline rows for it, and they void at settle
  with the revert as the reason. Morpho `0xaba3ac50`, at 100% utilization,
  voids the same way while it stays drained. Each venue's keys void at
  about 1.4 rows a day, so expect about three voids a day from these two.
- A v3 pool has no trailing baseline, so its best baseline is the advertised
  rate or the haircut.
- `record` quotes the venue before it checks the live key, so a refused
  forecast still costs one quote.
