# Playbook

How this agent picks and sizes an allocation. This file is the agent's, not
the operator's: edit it in the cycle where the evidence arrives, and say in
the retro which line changed and which settled row changed it.

Seed version, 2026-09-09. Everything below comes from
`journal/recon-2026-09-08.md`, its two corrections, and the settlement tests
in `core/tests/`. None of it comes from a settled allocation, because none
exists yet. Treat every number here as a prior with no evidence behind it.

## The one question

Does this position beat doing nothing, after gas? Not "is the yield high" -
`advertised_apy` is gross, instantaneous, and quoted before everything that
takes money away. The number being scored is `net_delta`, the gap between
what was forecast and what settled. A forecast that is right about a small
return beats a forecast that is wrong about a large one.

## How a cycle chooses

1. **Price the move first, not the yield.** `python3 core/cost.py` gives the
   round trip at today's gas. If it is above `max_round_trip_bps` in
   `strategy/risk.json`, stay idle and write why in `journal/cycles.md`. Gas
   moved from 0.1002 to 0.2288 gwei in a day, so this is a live input, never
   a remembered one.

2. **Filter before ranking.** Apply `strategy/venue-filters.json` to
   `journal/venues.jsonl` before looking at a single rate. Most venues on
   this chain are dust and dust produces the highest advertised numbers.

3. **Convert the rate into a hold.** `python3 core/cost.py --apy X --kind K
   --hold-days D` gives the break-even. At 250 USDG on Morpho, break-even is
   11.8 hours at 63% and 117.6 hours at 5%. If the break-even hold is longer
   than the horizon being considered, the position is a loss on entry.

4. **Forecast a gross rate the agent owns.** Take the advertised rate, apply
   the kind's `forecast_haircut` from `strategy/risk.json`, and adjust for
   what the quote shows: a Morpho rate falls as utilization falls, so a
   market at 95% utilization will not hold its rate for a week. Pass that
   number to `core/ledger.py open --gross-apy`; the engine derives the net
   and refuses the row if the costs do not support it. Passing the
   advertised rate straight through is legal, lazy, and will show up in
   `advertised_delta`.

5. **Size at full or not at all.** Cost is fixed in USDG, so a 50 USDG
   allocation pays five times the basis points of a 250 USDG one. Partial
   sizing is a way of paying more for less.

6. **Record idle explicitly, and keep its horizon short.** Whatever is not
   allocated goes in as an idle row. Without it, doing nothing is invisible
   to `core/score.py` and `idle_delta` has no baseline but zero. Give that
   row the shortest horizon in `horizon_choices_min`, 1,440 minutes, not the
   horizon of whatever was allocated beside it. Idle is the one position
   whose round trip is 0.0000 USDG at every size, so a long idle horizon
   buys nothing and forfeits the right to reconsider: the row holds its
   share of the bankroll until it settles. `a00007` (opened cycle 16,
   settled 2026-09-22) sat idle for seven days by choice, and the board
   changed under it more than once while it could not answer.

7. **Forecast the whole filtered quote, not the shortlist** (experiment
   `E-2026-11-28-a`, reopening the withdrawn `E-2026-11-25-a` under the
   fixed `graded_rows_per_day`). On a full cycle, record an agent forecast
   row (`core/forecast.py record`, all three horizons) for every venue the
   quote returned that passes `strategy/venue-filters.json`, not only the
   ones being considered for allocation. A refused horizon (row still live)
   is fine; move on. `blocked_venues` is part of that filter: never record
   an agent row on a blocked venue, even "as the rate belief". Arcadia Debt
   `0xf37c0c59` reverts every simulated withdraw, so its rows void
   (f002964, f002965 voided; DEEP-2026-11-26). In the 7 d to 2026-11-28 the
   baselines covered 18 venues and the agent 7, with 47 of 85 agent rows on
   one Morpho contract `0x9d53d5e3` (DEEP-2026-11-28). The rule number from
   `strategy/forecast_rule.py` is the default; it costs no extra quotes.

## What the evidence says today

- **The deep end of the board is unallocatable.** The 313M USDG Morpho market
  advertises about 4.5% and a 250 USDG seven-day hold forecasts under 2 bps
  net, which the operator's own 5 bps floor refuses. Clearing the floor over
  seven days needs roughly 6.1% APY, and over one day roughly 29%. Safety and
  legality point in opposite directions here, and that tension is the thing
  worth learning about.

- **LP positions are a bet on price, not on fees.** On a 250 USDG full-range
  position in the 33M pool, a 0.054% ETH move cost 0.0675 USDG while the
  whole share of fee growth over the same window earned 0.000028 USDG. The
  round trip is 3.4 times Morpho's. Do not open one without a reason to
  believe ETH is flat over the horizon, and say what that reason is.

- **A legal row can still be a bad one.** A 250 USDG Morpho allocation
  forecasting +88 bps over seven days is legal to write and realizes
  -0.135643 USDG if it is exited after two hours. The horizon is the
  decision. `min_hold_minutes` protects nothing.

- **The best-paying market is the one you cannot get out of.** Morpho's
  supply rate is the borrow rate times utilization, so ranking by rate ranks
  by how little free liquidity is left. Both markets cycle 1 could allocate to
  sat at 99.95% and 99.99999% utilization, the second with 33 raw units of
  free liquidity on 46,836 USDG. At 100% utilization a withdraw reverts, and
  a realized number taken from the share price would describe an exit that
  could not have happened. Check free liquidity against
  `min_free_liquidity_multiple` before the rate, not after.

- **A high rate that has only traded twice is not a rate.** The 63% market
  read 10.32% and 43.51% in the only two windows it traded in over two
  weeks. Treat a high number on a shallow market as a wide distribution, and
  size the horizon short enough that being wrong is cheap.

- **A Morpho rate read at a utilization peak is a peak, not a level.**
  `a00004` entered the 1.2M USDG market at 17.95% and 90.3% utilization, the
  IRM's target, and realized 10.28% gross over seven days: utilization fell
  to 62-77% and the AdaptiveCurveIRM lowered the borrow rate from 19.9% to
  12.4% APY while utilization sat under target. Both channels fall together
  after a spike. Read the market's recent utilization history before the
  rate: if utilization is at or above target after a swing, forecast the
  rate it will decay to, not the one on the screen. The 0.60 Morpho haircut
  in `strategy/risk.json` is that row's ratio, rounded up.
  The lesson runs one way only. `a00010` entered the same market at
  11.75% and 86.8% utilization, below target and rising, and realized
  about 36% gross: utilization spent enough of the week above 90% that
  the IRM raised the rate at target, and time above target pays up to
  four times that rate. Below target and rising, the rate climbs, so do
  not trim it for decay.

- **When a venue has a trailing realized rate, anchor on it, not the
  advertised one.** `a00016` (Flock, 70.7% advertised) was forecast at the
  0.75 haircut and realized 119%; the ledger's 1-day baselines had run
  120-144% since. An event-window advertised rate lags the share price.
  `a00015` also shows: do not trim a Morpho rate above target for decay
  the quote's history does not show. `a00020` trimmed anyway and missed by
  56 points. `a00021` anchored on Flock's trailing rate and then cut it by a
  quarter for credit risk and reversion, and missed by 43 points. Credit risk
  is a reason not to allocate, not a discount on the rate.
  `a00031` (aa586d26, settled cycle 502) realized 72% on a 100% forecast
  entered at a 162% spike: the market swings between a flat ~30% and a
  pinned 99.8%-utilization 275-340%, and a multi-day hold averages the
  two. Forecast it toward the trailing week and hold one day while pinned.
  `a00035` (settled cycle 525) forecast 200% above the trailing week and
  realized 104%: the pin broke within 12 hours. Not above the week.
  `a00038` (settled cycle 597) forecast 40% off the pin, under an 89%
  week, and realized 86%: it re-pinned inside three days. Not far below
  the week either; off the pin, forecast a multi-day hold near the week.
  `a00042` (settled cycle 668) forecast 75%, under an 89% week, and
  realized 172%: it went back above target inside three days again.
  Off the pin, forecast at the week, never under it - but only when the
  quote shows utilization near or above target. `a00061` (settled cycle
  890) forecast 55% at 79.6% utilization under a 94% week and realized
  36%: utilization fell to 67% and the IRM decayed the whole three days.
  Below target and falling, forecast at spot or under it, not the week.
  `a00072` (settled cycle 1086) trimmed a 21.4% advert to 19% at 60%
  utilization for a rate that had been falling, and realized 23.4%:
  supply left, utilization rose to 76%. A falling rate is not falling
  utilization; well below target, forecast at the advert, not under it.

- **The Morpho decay lesson does not carry over to a static-curve vault.**
  `a00008` (Senior ArcadiaV2 tranche, settled cycle 211) was trimmed from
  the 0.75 haircut's 13.58% to 13.0% for 92% pool utilization, and
  realized about 14.5% gross, 0.80 of advertised. Arcadia's rate curve
  does not adapt, so it decays on one channel, not two. Forecast the
  haircut rate on a vault unless the quote shows utilization already
  falling. `a00013` (same tranche, settled cycle 374) realized 0.79 of
  advertised, so forecast an Arcadia tranche between 0.80 of advertised
  and its trailing week, not at the vault haircut, which Flock's rows
  pull up. `a00027` (settled cycle 544) forecast 9.0% under a 10.5%
  trailing week and realized 12.0% gross, 1.20 of advertised: the 0.80
  rule missed upward. Forecast an Arcadia tranche at its trailing week. `a00066`
  (7 d, settled cycle 1032) forecast 13.5%, above its 13.9% trailing week's
  low end toward the advert, and realized about 10.6% as the rate decayed
  to 10%: never forecast a tranche above its trailing week. A tranche's `idle_assets` reads 0 because its exit is the
  pool's cash, so check the engine's exit simulation, not that field.

- **A credit vault's exit is its idle cash, and it can drain under you.**
  `a00036` (Flock, 3 d, settled cycle 683) passed its entry withdraw
  simulation, then Flock's idle USDG went to 0 and the row sat 110 hours
  past its horizon until repayments returned. Flock has no withdrawal
  queue. Enter it only while `idle_assets` is at least four times the size,
  at the 1-day horizon, so the exit is re-tested every day. `a00065` (Flock,
  3 d, entered with idle at 42 times the size) drained anyway and was
  loss-marked at -100% in cycle 1012, so Flock is now in `blocked_venues`.
  It paid back 108 h late (cycle 1038) at 44.5% net vs 41.9% forecast: the
  rate was right and the horizon failed, so the payback does not unblock it.
  For any other credit vault with no queue, the 1-day horizon is the rule,
  not a preference.

- **The rate forecast is measured against the trailing rates, not the
  advertised one.** With `f000187` voided as a units slip (operator note,
  2026-10-06), cycle 495's score over 106 agent rows reads: mean absolute
  error 6.63% for the agent, 8.07% advertised, 5.49% trailing 7-day, 4.76%
  trailing 24-hour. Beating the advertised rate is the floor, not the bar.
  On vaults the trailing day is best by far (3.47% against the agent's
  8.68%). Cycle 525's score puts the gap outside noise: over 57 vault
  agent rows the agent's error exceeds the trailing day's by +5.78%
  [+0.55%, +11.01%], winning 29% of rows against it (`f000520`, 100%
  forecast against 160% realized, is the type). So a vault forecast is the
  quote's trailing 24-hour rate, at every horizon, and the note names it;
  leave it only for a state change the trailing day cannot yet show (a
  depositor exit, a utilization break), and name that change. On Morpho the agent is level with the advertised rate (4.94%
  against 5.32%). Record rates as fractions: 0.049 is 4.9%.

## What would change this file

The first settled deployed row arrived on 2026-09-17 (`a00004`), and it
could tell the three failures apart: the rate was wrong, the horizon was
right, and the cost was wrong in the favorable direction because gas fell,
which no strategy input controls. One row moves a prior; it does not settle
one. The second Morpho settlement (`a00010`, 2026-09-24) realized about
3.1 times advertised against `a00004`'s 0.57, so two rows bracket the
0.60 haircut from both sides and say the snapshot is the weak input, not
the multiplier. The haircut stays until a third row. The first vault settlement (`a00008`, 2026-09-24) realized 0.80 of advertised
against the 0.75 vault haircut; a second vault row decides whether to move
it. The third Morpho row (`a00015`, 0.99) and the second vault row
(`a00016`, 1.68) arrived 2026-09-27; haircuts moved to 0.75 and 0.85.

