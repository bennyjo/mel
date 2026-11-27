# Operator notes

Evidence and instructions from the operator to the agent, newest first. The
agent reads this at the start of every cycle and never edits it.

## 2026-11-27: `graded_rows_per_day` now counts only rows that began in the window; `E-2026-11-25-a` is withdrawn

**The metric was wrong, not your experiment.** `graded_rows_per_day` placed
each row at its settle time, so a window counted rows recorded before it
started. Of the 27 agent rows it graded after the `E-2026-11-25-a` commit
(`e523ac4`, 2026-11-25T08:43:45Z), 18 were recorded before that commit. That
is why it read 13.0 and looked on track to KEEP while recording fell: by
`agent_rows_per_day`, 12.9 a day in the 7 days before the commit and 10.1 a
day since.

**The fix.** `core/experiments.py` now starts each graded row at its
`entry_ts` and ends it at its `settled_ts`, as `agent_vs_best_mae` and
`abs_net_delta` already do. A days window counts only the rows that began and
settled inside it, on both sides of the commit. The 7-day baseline before
`e523ac4` is 7.0, not 10.14.

**What I did, and why.** With the fixed metric, `grade` printed REFUSED for
`E-2026-11-25-a`, because its stored baseline was 10.14. `CYCLE.md` step 5
says a REFUSED experiment is reverted and closed `withdrawn`, so I did that
in this operator commit to keep CI green: playbook step 7 ("Forecast the
whole filtered quote") is reverted, and the close row is in
`strategy/experiments.jsonl`. There is no verdict row: `grade` prints none
for REFUSED, and the ledger accepts none.

**You may reopen step 7** as a new experiment: re-add the step in its own
commit, then run `propose --metric graded_rows_per_day --window days=7
--commit <sha>` for the baseline (it read 7.0 at `e523ac4`). It needs no
`prior`, since it was withdrawn, not rolled back. Two things to carry over:

- The DEEP-2026-11-26 sentence went with the revert: never record an agent
  row on a venue in `blocked_venues`. Arcadia Debt `0xf37c0c59` reverts every
  simulated withdraw, so its rows void.
- Rows settle 1, 3 or 7 days after entry, so the first days of a window read
  low until they settle. Set the bars against the new baseline, which
  has the same lag.

## 2026-11-25: the runner missed no ticks; forecast-rule misses get a ranked list; `f002963` stays

This answers the deep retro's 2026-11-25 runner ask and your cycle 1535 ask
about `f002963`, and starts a list so the forecast-rule misses your hourly
retros find reach a deep retro's ranking.

**The runner didn't miss ticks.** `origin/lease` shows 25 of 25 hourly
acquires and releases from 2026-11-24 08:00Z to 2026-11-25 08:15Z, including
every hour in the two gaps you saw (14:11-19:23Z and 20:07-01:18Z). The
ticks with no commit were light ticks that settled, voided and recorded
nothing, so `CYCLE.md` ("0. Sync and read", the no-op light tick exception) has them write no log
line and make no commit. The cycle count follows how many rows come due, so
it isn't a confound for `E-2026-11-25-a`. Don't chase it further.

**Forecast-rule misses now go on a list.** Four hourly retros since 11-23
left a `strategy/forecast_rule.py` change for the deep retro, and no deep
retro has ranked any of them. From now on:

- When an hourly retro leaves a `forecast_rule.py` change for the deep
  retro, it appends one line to the "Forecast-rule candidates" section of
  `journal/proposals.md`: the rows, the miss, and the mechanism. Add new
  rows to an existing line when they show the same mechanism.
- Each deep retro ranks that list. When `core/replay.py` supports the top
  item, open it as a `replay_pass:strategy/forecast_rule.py` experiment.
  Mark an item done or dropped on its line, with the experiment id or the
  reason.

The list is seeded with the three mechanisms from 11-23 to 11-25: the
post-pin AdaptiveCurveIRM decay on Morpho 0xaa586d26 (`f002824`, `f002776`,
`f002878`), the lumpy-vault 1 d fallback on Arcadia Senior (`f002898`), and
the 7 d rate decay on vault 0x37788ff0 (`f002640`). A replay-graded change
doesn't move `graded_rows_per_day`, so it can run beside `E-2026-11-25-a`.

**`f002963`: leave it.** You said voiding an over-forecast that will
probably score badly is the act the agent must not choose. The same applies
to an operator void before the row settles. The ledger carries the slip, and
your process fix, working the rule from the live trailing read, is already in
effect from cycle 1535.

## 2026-11-07: please lower `min_expected_net_bps` to 5, the operator floor

**The ask.** Set `min_expected_net_bps` in `strategy/risk.json` from 10 to 5,
the floor in `config/protected.json`. The file is yours, so make the edit in
your next full cycle and commit it like any other strategy change.

**Why.** More than half of your recent allocations were idle: 7 of the 11
opened in the last 7 days. Half the bankroll sits idle now in `a00085`. An
idle row teaches nothing about forecasting, so only deployed rows produce a
`net_delta` lesson. Your retro 2026-11-07-0730 names the two markets that
missed the 10 bps floor: Morpho 1efd13a2 at 8.8 bps net and Purinta at 9.4 bps
net, both at 7 d. You have never held either one.

**What it costs.** Mel runs on paper (`real_execution_enabled` is false), so
no money is at risk. A 5 bps margin is close to your recent forecast error, so
some of these rows may finish under idle. Score them like any other row. Every
other rule still applies, including the free-liquidity checks and the caps.

## 2026-10-28: a position stuck 24 h past its horizon is loss-marked; `a00065` marks at zero

This answers your cycle 966 ask about `a00065`. You asked whether to keep
waiting or to add a loss-marking rule to `core/settle.py`. The answer is the
rule, and it replaces the 2026-10-13 "stays open" decision for every stuck
position from here on.

**What changes.** `core/settle.py run` still leaves a row open while the
venue can't pay it back. Once the row is `loss_mark_after_horizon_minutes`
past its horizon (1,440, so 24 h, in `config/protected.json`), `run` marks
it instead:

- The row gets status `marked` and a `mark_reason`.
- It's booked at what the venue could pay a withdrawer at that block: free
  liquidity for a Morpho market, and for a vault the simulated withdraw,
  which counts as zero when it reverts. No exit gas is charged when nothing
  comes back.
- `core/score.py` scores it like a settled row, so the loss shows in
  `net_delta`.
- It no longer counts against the bankroll, the open-position count, or the
  venue share.

**A mark isn't final.** `due` lists every marked row and `run` re-checks
it. When the venue pays it back, the row settles over its real hold, the
mark moves to `loss_mark` on the row, and the score moves with it. `a00036`
did exactly that, 111 h past its horizon, at +125% net APY, so a mark that
couldn't be undone would have recorded a loss that never happened.

**`a00065`.** It was about 81 h past its 72 h horizon on 2026-10-28 and the
Flock vault holds 0 USDG idle, so the first `run` after this lands marks it
at zero: gross -250.000000, net -250.025473, -100% net APY. Log that once,
with the mark reason, and let the retro cover it like any other settled
loss. After that, mention it in the log only as "`a00065` marked, re-checked
each run", and write about it again only when it settles. A marked row that
is still stuck isn't a failure, so don't count it or propose it.

**Why 24 h.** The forecast ledger voids an unreadable row six hours past its
horizon, but a forecast has no capital behind it. A drained vault refills
from repayments over days, so a position gets a full day of hourly retries
before it's marked. The re-check means the mark costs little if it's early.

## 2026-10-13: `a00036` stays open and retries cheaply; settle gets a time budget

This answers your cycle 574 and cycle 643 asks about `a00036`, and your
cycle 505, 514, 615, 641, and 643 asks about `core/forecast.py settle`.

**What `0x96d80433` is.** It's `InsufficientAssets()`. The Flock Credit
Vault (`0xd42174d3db28b0fa2bd25381c3521b18ae9db490`) holds 0 USDG idle out
of about 64,000 USDG in assets. It drained between 2026-10-07 and
2026-10-09, before `a00036` matured. Borrowers still pay interest, so
`totalAssets` grows about 138 USDG a day, but none of it stays in the
vault. The vault has no withdrawal queue, so there is nothing to request
and wait on. A withdraw passes only after borrowers repay or new deposits
arrive. The engine was right to refuse every settle. `f000828` voided for
the same reason.

**Decision: `a00036` stays open.** Keep retrying it in step 1 as you do.
When the vault can pay it back, it settles over its real hold, and the
score shows the horizon miss honestly. Don't void it, and don't propose a
void. Its 250 USDG and the Flock slot stay committed until then. That's
the cost of an exit that the chain won't give, and it belongs in the
record.

**The retry gets cheap.** An engine change is in review that stops each
`a00036` retry from scanning seven days of logs twice (about 6,150 RPC
calls and 3 minutes an hour). Settlement first retries the withdraw from
holders already known to hold enough shares, and it skips the vault rate
scan that settlement never uses. A position still settles only when a
simulated withdraw succeeds.

**`forecast.py settle` writes as it goes.** A second engine change is in
review: `settle` writes each venue's rows as it settles them and stops
starting new venues past a time budget, leaving the rest honestly due for
the next run. Until it lands, keep settling a stalled batch row by row
with `--id`, as cycle 643 did.

**Stop counting `a00036` failures.** From this cycle on, don't restate
the failure as a numbered failed settle in each log entry or commit
subject, and don't add a `journal/proposals.md` entry for it. The
operator knows. In the log, list it once under "Open" as "`a00036` held
open: the Flock vault has no idle USDG". Write about it again only when
something changes: it settles, the revert reason changes, or the vault's
idle USDG reaches 500 USDG and the withdraw still fails.

## 2026-10-09: the runner lease is on `main` now, and a run without it stops

Your cycle 519 proposal is actioned. You were right that the lease had
never held: every cloud push to `refs/mel/lease` failed while the same
session's push to `main` worked, so every cloud cycle ran unprotected and
cycles 518 and 519 settled the same forecast rows. The lease is
`journal/lease.json` on `main` now, so any credential that can push `main`
can take it. `core/lease.py` writes it with one `lease:` commit per acquire
and per release, each a plain fast-forward push, so two runs that race for
it cannot both win. There is no more `"written": false` cycle.

What changes for you, cloud runner:

- Run `python3 core/lease.py acquire` in step 0 as before. When it prints
  `"acquired": false`, stop the invocation: settle nothing, log nothing,
  commit nothing. That covers a fresh lease held by another run and a
  lease that could not be written (origin unreachable, the push refused,
  or a local `main` behind or diverged from `origin/main`).
- Each cloud run has its own id, `cloud-` plus a random suffix, so the
  next hourly run is kept out while a slow settle is still going. The
  lease lasts 110 minutes, longer than cycle 518's 80.
- Run `python3 core/lease.py release` at the end of step 8 as before, and
  also when the push failed.
- The collision guard looks past `lease:` commits. Never edit
  `journal/lease.json`.

## 2026-10-06, later: Mel reads the chain through the Olas RPC gateway

`config/protected.json` points `rpc_url` at
the Olas RPC gateway, the endpoint Pearl uses for
Connect services on this chain. It ran a full cost quote with no throttling
and serves state well past the public endpoint's ten-minute window. Expect
far fewer `eth_getLogs` refusals and far fewer voids. If you see the
gateway throttle or refuse, say so in `journal/proposals.md` with the count
from `journal/rpc-usage.jsonl`; do not route around it.

## 2026-10-06: a units guard on agent forecasts, and f000187 is void

You caught it first, in cycle 297's log: `f000187` was recorded as `4.9`
meaning 4.9%, so it stored as 490% and doubled your mean error. It is void
now, with the reason on the row and the realized fields kept. This is the
one kind of void that is honest on a forecast that settled: the number was
never your belief. A forecast that turned out badly is never voided.

`core/forecast.py record` now refuses an agent forecast above
max(3 x advertised, advertised + 0.5) unless you pass `--confirm-extreme`.
If a real spike is your belief, pass the flag and say why in the note.

With the row gone, the honest standing is: you beat the advertised rate,
you tie or trail the trailing 7-day rate, and you lose to the trailing
24-hour rate. That is the result, and it is worth writing down in the
playbook as what you are measured against next.

## 2026-09-28: the divergence of cycles 298 to 300 is reconciled, and loop.sh now does it itself

You were right to refuse to settle on the stale state. `a00018` was already
settled on origin at block 73,965,702 while this machine slept, and the
uncommitted edit left in the working tree was a second settlement of it at
73,967,413. It is discarded, with a copy kept under `work/`. Your three
blocked-cycle entries and the telemetry commit are rebased on top of origin,
and the ledger is origin's, untouched.

How it happened: the laptop slept, the lease went stale, and the cloud
correctly took over (cycles 291 to 297). On waking, this machine still held
an unpushed commit and a dirty tree, and `loop.sh` only knew how to
fast-forward, so it handed you a diverged checkout and a tool allowlist
with no `git merge` or `git restore` in it. Your ask was for that
permission; the fix goes one level lower instead. `loop.sh` now:

- saves any uncommitted journal edits to `work/stale/` and drops them
  before syncing, because committed work is the record;
- rebases this machine's commits onto origin when they touch no ledger
  file (the prose journals union-merge);
- otherwise skips the cycle and gives the lease back, so the cloud keeps
  covering until the operator reconciles.

So a session never starts on a diverged or stale checkout. If you ever
find one anyway, stop and say so, exactly as cycle 298 did.

## 2026-09-24, later: your rate forecasts are now scored without capital

Settled allocations teach you about two rows a week. From today,
`journal/forecasts.jsonl` scores the part that needs no capital: the rate
you expect a venue to pay, against what it paid. `core/forecast.py` writes
it and nothing else does. `CYCLE.md` changed where it touches the ledger:

- Step 1 settles due forecast rows on every tick, light ticks included.
- Step 2 has a forecast section. Your skill is your error against the best
  of four mechanical baselines: the advertised rate, the venue's trailing
  day and week, and the advertised rate times your haircut. Don't move
  `strategy/` on it until it says anything other than "too few to say".
- Step 5's quote records baseline rows by itself. Never pass `--no-record`.
- Step 6 comes before any allocation decision. Record your gross rate at
  all three horizons for every venue you researched, whether or not you
  allocate to it. A key with a live row is refused, and that's the rule.

The ledger refuses a venue whose own rate is over 1000%, because it moves
with a price: the Arcus 3x Long vaults. The first nine `agent` rows, marked
`cycle: operator-proof`, are the operator's end-to-end proof, not yours.
For what the ledger measures and how fast, see
`journal/forecast-ledger-2026-09-24.md`.

## 2026-09-24: both of today's proposals are in

- Cycle 216, Flock: `core/ledger.py` now refuses a vault entry with no
  `share_price`, and `core/venues.py` prices the share from
  `totalAssets / totalSupply` when `convertToAssets` returns nothing. Flock
  should now quote with a usable share price; check it live before you
  size into it, and say which source the row used (`share_price_source`).
- Cycle 211, the missing retro: CYCLE.md step 3 now covers every row
  settled since the newest retro file, so a row a light tick settles gets
  its retro at the next full cycle. Cycle 216 already did this for
  `a00008` by judgment; now it is the rule.

## 2026-09-23: one runner writes per hour, and the lease now says so

Your cycle 135 escalation is answered, with the third bullet done properly
rather than the first. You were right that a non-holder "would also do it,
but only if it also skips settling". It now skips everything.

What changed. The lease used to mean "a cycle is in flight", so it covered
about four minutes an hour and both runners woke outside it. It now means
"a local loop is alive": `loop.sh` takes it with a TTL of ninety minutes,
refreshes it at the top of every cycle, and releases it only when the loop
exits. So while the laptop loop runs, the cloud sees the lease held for the
whole hour; when the laptop sleeps, the lease goes stale within ninety
minutes and the cloud takes over. That is the intended division: the local
loop has priority, the cloud covers the gaps.

A runner that finds the lease held by the other now **stops**: no settle,
no score, no cycle-log entry, no commit, no push. CYCLE.md step 0 says so
and `loop.sh` skips the session outright. One runner writes per hour, so
the cycle numbering stops colliding and `a00008` on 2026-09-24 01:18 UTC
cannot be settled twice.

Two smaller things. `.gitattributes` gives `journal/cycles.md`,
`journal/proposals.md` and `journal/rpc-usage.jsonl` a union merge, so a
prose divergence resolves itself; `journal/allocations.jsonl` deliberately
does not get one, because a union merge of two settlements is exactly the
silent corruption we are avoiding. And `loop.sh` now commits the
`rpc-usage.jsonl` that `core/` writes mid-session before it retries a
rebase, which is what made `git pull --rebase` refuse outright on
2026-09-22.

Your reconciliation work through cycles 135 to 178 was correct every time,
and the record of it in `journal/cycles.md` is why this was diagnosable.

## 2026-09-21, later still: the lease verdict is in your prompt now

Cycle 113's proposal, done both ways you suggested. Under `loop.sh` the
first line of your prompt reads `Runner context from loop.sh: MEL_LEASE=...,
MEL_PUSH_BY_LOOP=1`, and `python3 core/lease.py env` prints both variables.
In the cloud that line is absent and `env` prints nulls, so you acquire
the lease yourself; origin refuses the cloud's write, so you proceed
unprotected and say so, which the 11:15 UTC run already did correctly.
The local loop's lease still protects the local side: the cloud honours a
lease the laptop wrote.

## 2026-09-21, later: a runner lease, so the laptop loop and the cloud can coexist

`loop.sh` may run beside the cloud routine again, as Phil's does. Two
things keep them from double-cycling. `core/lease.py` puts a lease on
`origin` (`refs/mel/lease`) for the length of one cycle; whichever runner
finds a fresh lease held by the other runs a light tick. `loop.sh`
acquires and releases it for you and tells you the verdict through
`MEL_LEASE`; in the cloud you run `acquire` and `release` yourself, as
CYCLE.md steps 0 and 8 now say. If origin refuses the lease write, you
proceed and say so; a lease you cannot write protects nobody and must
block nobody. `loop.sh` also refuses to start when another copy is running
in the same checkout.

## 2026-09-21: you now run in the cloud, hourly, like Phil

The operator's laptop sleeps when its lid is closed and no machine here is
always on, so from today an hourly cloud routine runs CYCLE.md instead of
`loop.sh`. What changed for you: step 0 now syncs (unshallow, fetch,
fast-forward, and a collision guard that demotes you to a light tick if
another runner committed a cycle in the last 20 minutes), and step 8
pushes unless `MEL_PUSH_BY_LOOP` is set, which only `loop.sh` sets. The
cloud has no keyring, so you push and verify the push yourself. The local
loop is stopped; if the operator runs it again by hand, the collision
guard is what keeps the two from double-cycling.

## 2026-09-21: loop wakes on the hour; refresh is yours; reference_pool says why

Three of your proposals, answered.

- Phase (cycle 106): `loop.sh` now wakes two minutes past the next hour
  boundary instead of sixty minutes after the session ended, so a tick
  lands in every hour and a full-cycle hour cannot be stepped over. Your
  choice to take the marks on the light tick was right.
- Record age (cycle 79): count hourly ticks, as `strategy/schedule.json`
  says. From now on you refresh the record yourself at a full cycle when
  it is older than about three days; CYCLE.md step 5 says how. The
  operator refreshed it today as well.
- `reference_pool` (cycle 74): the `VenueError` now carries the RPC error
  it swallowed, and `core/cost.py` prints the `rpc` counters on failure
  too.

Also: a second copy of `loop.sh`, started on 2026-09-17 in a terminal,
had been running beside the first without ever producing a cycle. Both
were stopped today and one was started clean. The 24-hour outage on
2026-09-20 and the 8-hour one last night were the machine sleeping, not
you.

## 2026-09-17, later: a throttled vault scan now says so

Done, from your cycle 62 proposal. When the vault rate scan is throttled,
every vault row's `rate_error` reads `vault rate scan throttled: ...`
instead of the no-history text. A single-venue `quote --venue` re-read is
the right response to the first and a waste on the second. Chunking the
scan itself is still open on the operator's side.

## 2026-09-17: validate fixed, a00006 void, and the answer on repair cycles

`core/validate.py` now recomputes `realized_net_apy` from the exact hold the
row's own `exit_ts` and `entry_ts` give, the same expression the writer
uses, so `a00004` and every later row reproduce to the last digit. Your
diagnosis in `journal/proposals.md` was right in size and in sign. The
seven repair cycles it cost are on the operator.

`a00006` is void, with your reason on the row. Your first Morpho voids and
this one are the same class: a venue whose views answer and whose
state-changing calls do not. The engine now checks a vault's exit by simulating it. `core/ledger.py
open` and `core/settle.py` quote the one venue with a simulated
`withdraw` of `min_free_liquidity_multiple` times the size, called from
the vault's largest recent depositors, and refuse when every one reverts.
That is the check, not the vault's own USDG balance: the Senior tranche
behind `a00008` holds zero USDG itself and withdraws fine, while the debt
token holds 7,246 USDG and reverts for everyone (`0xced4f633`). The
outcome is recorded in `settle_state.exit_simulation` on the row.
`python3 core/venues.py probe --venue <prefix>` runs the same simulation
by hand, plus `deposit`, and prints the revert data verbatim; use it
instead of `curl`. A throttled vault rate scan no longer kills the quote:
vault rows come back with `rate_error` and the rest of the board stands.
Keep the debt token blocked in `strategy/venue-filters.json` anyway; two
checks are better than one.

The standing question: yes. A repair cycle that cannot clear the failure
commits its log entry and its proposal text anyway, with a subject that
says it is a repair cycle. The log is the evidence the operator reads; an
uncommitted log is invisible, and a dirty working tree is what stops
`loop.sh` from fast-forwarding over the operator's fix. Never commit a
ledger write over a failing validate; a log entry is not a ledger write.

`journal/venues.jsonl` is being refreshed today.

## 2026-09-15: cycle 11 ended mid-way; the loop was down for five days

The loop did not run between 2026-09-10 06:47 and 2026-09-15 07:49 UTC, which
is why `a00003` settled 3.3 days past its horizon. That was the operator's
machine, not you. `a00004` matures 2026-09-17 01:14 UTC and the loop is up.

Cycle 11 settled and wrote its retro, then ran the live quote in the
background and ended its turn to wait for it. A headless session ends with
its turn, so the quote, the forecast, the allocation, and the cycle-log entry
never happened. `CYCLE.md` now says so under "How this session runs": run
slow commands in the foreground with the tool timeout at 600000 ms, never in
the background, and always finish with the `journal/cycles.md` entry.

The 500 USDG that `a00003` freed has no row. At the next full cycle (13:00
UTC), allocate it or record it idle, and write the cycle 11 entry you owe
`journal/cycles.md` as part of the cycle 12 entry.

## 2026-09-10: `--rank-by rate` is in, and the depth floor now runs before the pick

Your second proposal is done. `core/venues.py quote --top N --rank-by rate`
picks the N highest advertised rates at the last refresh; `--rank-by depth`
is the old behavior and the default. `--min-depth` is applied to the record
before the pick, so `--top 20 --min-depth 10000` is now twenty rows that
pass the floor, not twenty deep rows minus the ones that fail it. CYCLE.md
step 6 shows the flag. `core/tests/test_venues.py` covers the ranking.

One thing to expect from `rate`: a recorded vault rate is inferred from
Deposit and Withdraw events and today's record has one at 2,021%. The live
quote is the number; the recorded one is a hint. Your haircut for vaults in
`strategy/risk.json` already says as much.

## 2026-09-09: your liquidity rule is now a protected cap, and cycle 1's two positions are void

You were right in `strategy/playbook.md`: the best-paying Morpho market is the
one you cannot get out of. That check now lives in the engine, not only in
your filters. `config/protected.json` carries `min_free_liquidity_multiple`
(2.0). `core/ledger.py` refuses a Morpho entry whose free liquidity is under
that multiple of the size, and `core/settle.py` refuses to settle an exit the
market could not pay, leaving the row open until it can. Your 4.0 in
`strategy/venue-filters.json` is tighter and stays yours; `core/validate.py`
fails the build if it ever drops under 2.0.

`a00001` and `a00002` are void, with the reason on each row. Both sat at
99.95% and 99.99999% utilization at entry, so no honest exit existed. They
teach nothing about forecasting and must not count as losses avoided. The
idle row `a00003` stands. Your next cycle starts from an empty book: quote,
filter on free liquidity first, and allocate or stay idle on the numbers.

The public endpoint findings from the recon memo (ten-minute state window,
60-call batch limit, 10,000-row `eth_getLogs` cap, ERC-4337 traffic) are
folded into `CHAIN.md`.

## 2026-09-08: chain reconnaissance before the first build

Everything in `CHAIN.md` was verified by hand today. The points that matter
most for the engine:

- The public RPC returns a JSON-RPC `429` body after a handful of calls in
  quick succession. Batch requests and exponential backoff are mandatory.
  The endpoint lives in `config/protected.json` and `MEL_RPC_URL` overrides
  it; a dedicated RPC is expected within one to two weeks, so the engine must
  not assume the public cap forever.
- The explorer API is behind a Cloudflare challenge. Do not build anything
  on it. Discover venues from RPC logs and contract reads.
- No Olas registries or mech marketplace exist on chain 4663, so there is no
  Pearl Connect service on this chain yet and no mech to ask. Real execution
  stays dormant. Build `core/real.py` against the Pearl Connect MCP surface
  (`wallet_info`, `preflight_transaction`, `safe_transaction`,
  `transaction_status`) so it is ready the day the registries land, and make
  it refuse to run while `real_execution_enabled` is false.
- USDG has 6 decimals and about 699 million in supply on this chain. It is
  the quote asset. Every USDG amount in the ledger is a decimal string in
  whole USDG, never a raw integer.
- Venues to look for first: Uniswap (any version deployed here), Morpho, and
  Pons. Do not assume canonical addresses from other chains; find the
  factory or the PoolManager on 4663 from creation events or from a
  verified address and record it in `journal/venues.jsonl` with the block
  it was confirmed at.
