# Mel (self-improving yield allocator, the honeybee)

A self-improving capital allocator for Robinhood Chain (chain ID 4663). The
agent (Claude Code, headless) runs `CYCLE.md` repeatedly: settle matured
allocations against on-chain reality, score its own forecasts, write a
retrospective, edit its own strategy, then forecast and allocate again.

## The question the repo exists to answer

Every venue advertises a yield. None of them settle it. An advertised APY is
gross, instantaneous, and quoted before gas, swap cost, rate decay, and
impermanent loss. Mel commits a forecast of the **net** return before each
allocation, then settles that allocation against realized on-chain state and
scores three numbers against each other:

- what the venue advertised,
- what the agent forecast,
- what the position actually netted.

The honest metric is `net_delta`, not profit: does the agent's forecast beat
both the advertised number and a do-nothing baseline, after costs? Profit on
a rising market proves nothing. Beating your own prior does.

## Layout

- `core/` and `config/protected.json` are the PROTECTED engine: RPC access,
  venue discovery, quoting, the ledger, settlement, scoring, and validation.
  The agent must never edit these. `loop.sh` reverts any such change.
  Also operator-owned: the top-level docs, `LICENSE`, and `.github/` (CI).
- `strategy/` is the agent's own playbook, risk policy, venue filters, and
  cadence. This is what self-improves. Its git history is the product.
- `journal/` holds the ledger (JSONL, written only by `core/`), retros, the
  cycle log, and `operator-notes.md`, which is how evidence reaches the
  agent. Asks that only the operator can act on go in `journal/proposals.md`.
- `CHAIN.md` records verified facts about chain 4663. Operator-owned.
- `CYCLE.md` is the per-cycle procedure the headless agent follows.

## Honest-simulation rules

These are what make a simulated result worth writing about. Breaking one
silently is the worst failure this repo can have.

- Price every entry and exit at live on-chain state read at that moment,
  including the swap's own price impact against real pool liquidity.
- Charge real gas. Read the block's gas price, convert to USDG, and subtract
  it. A move that costs more than it earns must show as a loss.
- Never record an advertised or quoted APY as a realized outcome. Realized
  return comes from settled on-chain state at settle time and nothing else.
- Record the forecast before the allocation, never after. A forecast written
  after the outcome is known is not a forecast.
- Leaving capital idle is always a legal move and is scored like any other.
- A position must be exitable. A Morpho market must hold free liquidity
  of at least `min_free_liquidity_multiple` times the size at entry and at
  exit, and a vault must pass a simulated withdraw of that amount from a
  real depositor, or the engine refuses it. A settlement that describes a
  withdraw the chain would revert is not a settlement.

## The boundary

Commits whose subject starts with `operator:` are operator commits and may
touch protected paths. Every other commit is an agent commit and must not.
CI enforces this independently of `loop.sh`, because the loop can be bypassed
or buggy and CI on the pushed range cannot.

## Real execution

Dormant. Real allocation runs through Pearl Connect, which signs as the
service safe so the agent never holds keys. Pearl Connect runs on chain 4663
(see `CHAIN.md`); real allocation stays off until the operator turns it on.
`core/real.py` is the only code that may touch funds. `real_execution_enabled`
and the `real` caps block are validated by CI.
