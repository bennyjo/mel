"""Settlement: what an open allocation actually netted, from on-chain state.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

Owns:
  - which open rows are due. A row settles when its forecast horizon has
    passed, and never before `min_hold_minutes`, which `core/ledger.py`
    enforces a second time at write time;
  - the arithmetic that turns two on-chain states into a realized gross
    return, per venue kind. Nothing here reads a rate. A realized number comes
    from the position's own state at entry and at exit and from nothing else,
    which is what keeps `realized_net_apy` honest against `advertised_apy` and
    `forecast_net_apy`;
  - charging the exit. Gas is read at the settle block, at that block's gas
    price, converted to USDG through the deepest live pool. A position whose
    yield does not cover its gas settles as a loss, and that is the point;
  - the loss mark. A position the venue still cannot pay back
    `loss_mark_after_horizon_minutes` past its horizon is marked: booked at
    what the venue could pay a withdrawer at that block, which for a drained
    vault is zero, and scored. A mark is a valuation, not an exit, so every
    run re-checks the row and settles it over its real hold when the venue
    can pay it back (a00036 did, 111 h past its horizon, at +125% net APY).

How each kind realizes:

  morpho_market
      Share price is `totalSupplyAssets / totalSupplyShares`. Morpho only
      moves that state when someone touches the market, so two stored prices a
      day apart can be identical while interest accrued the whole time. A real
      `withdraw` accrues first, so settlement replays Morpho's own accrual -
      Taylor-compounded borrow rate on the borrow side, fee shares minted to
      the fee recipient - up to each state's own block before taking the
      ratio. Without the recorded borrow rate it falls back to the stored
      price and says so.

  erc4626_usdg_vault
      Share price is `convertToAssets(one share)`, read at entry and at exit.
      Six decimals of share price is coarse (see `CHAIN.md`), so a short hold
      on a big vault can realize exactly zero. Zero is the honest answer;
      it is not an error. Entry and exit fees, if the vault charges any, are
      outside this number and make it optimistic by roughly the fee.

  uniswap_v3_pool
      A full-range position, which is the constant-product case of v3's maths
      and matches what `core/cost.py` charges to enter (swap half, mint).
      Liquidity is fixed at entry so that the position is worth `size_usdg`
      there; at exit it is revalued at the exit `sqrtPriceX96`, so impermanent
      loss falls straight out of the arithmetic, and fees are the position's
      share of the pool's `feeGrowthGlobal` over the hold. Fees are credited
      at the global rate, which a full-range position only earns while the
      price is in whatever range the fees were generated over; on a pool whose
      liquidity is concentrated this reads high, so the LP number is the least
      conservative on the board and is labelled so.

  idle
      Zero, minus nothing. Holding USDG on 4663 pays nothing and costs no gas
      to hold, so an idle row realizes exactly zero and is scored like any
      other row.

There is no strategy judgment here. This file does not choose when to exit -
the horizon on the row does that, and the row was written before the outcome
was known.

Commands:
    python3 core/settle.py due [--json]
    python3 core/settle.py run [--id a00001] [--dry-run] [--json]
    python3 core/settle.py explain a00001        # offline, replays the maths
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import abi, cost, ledger, rpc, venues

Q96 = 1 << 96
Q128 = 1 << 128
WAD = 10 ** 18
UINT256 = 1 << 256


class SettleError(Exception):
    """A row cannot be settled honestly from the state available."""


class Unexitable(SettleError):
    """The exit state was read and the venue cannot pay the position back."""

    def __init__(self, row_id: str, why: str) -> None:
        super().__init__(f"{row_id}: {why}. A withdraw would revert, so the position "
                         "stays open and keeps accruing; settle it when the venue can "
                         "pay it back")
        self.why = why


def _cfg() -> dict:
    return rpc.protected_config()


def _int(state: dict, key: str) -> int:
    """A raw integer out of a recorded state block. Recorded states hold
    strings, because a raw on-chain integer does not survive JSON as a
    number."""
    value = state.get(key)
    if value is None or value == "None":
        raise SettleError(f"recorded state is missing {key}")
    return int(str(value))


def _usdg(raw: int) -> str:
    return abi.fixed_to_str(raw, _cfg()["quote_asset_decimals"])


# --------------------------------------------------------------------------
# Morpho: the market's own accrual, then the share price
# --------------------------------------------------------------------------

def _taylor_compounded(rate_wad: int, elapsed: int) -> int:
    """Morpho's `wTaylorCompounded`: the first three terms of `exp(x)-1`.

    Copied in behaviour, not in spirit: this has to match what the contract
    would have credited, so it uses the same truncating integer maths and the
    same three terms rather than a floating-point exponential.
    """
    first = rate_wad * elapsed
    second = first * first // WAD // 2
    third = second * first // WAD // 3
    return first + second + third


def accrue_morpho(state: dict, to_ts: int) -> dict:
    """The market state Morpho itself would have written at `to_ts`.

    A real withdraw calls `_accrueInterest` before it pays out, so settling
    against the stored state would under-report a market that nobody has
    touched since the allocation was opened. Interest goes to the supply side;
    the market fee is minted as fresh shares, which dilutes the supplier, so
    both sides of the ratio move.
    """
    out = dict(state)
    rate = state.get("borrow_rate_wad")
    if rate is None:
        return out | {"accrued": False,
                      "accrual_note": "no borrow rate was recorded with this state"}

    last = _int(state, "last_update")
    elapsed = int(to_ts) - last
    if elapsed <= 0:
        return out | {"accrued": True, "accrued_seconds": 0}

    supply = _int(state, "total_supply_assets")
    shares = _int(state, "total_supply_shares")
    borrow = _int(state, "total_borrow_assets")
    fee = _int(state, "market_fee")
    if borrow == 0:
        return out | {"accrued": True, "accrued_seconds": elapsed, "interest": "0"}

    interest = borrow * _taylor_compounded(int(str(rate)), elapsed) // WAD
    supply += interest
    borrow += interest
    if fee:
        fee_assets = interest * fee // WAD
        # Morpho's `toSharesDown` against the market excluding the fee itself.
        fee_shares = fee_assets * shares // max(supply - fee_assets, 1)
        shares += fee_shares
    out.update({"total_supply_assets": str(supply), "total_borrow_assets": str(borrow),
                "total_supply_shares": str(shares), "last_update": str(to_ts),
                "accrued": True, "accrued_seconds": elapsed, "interest": str(interest)})
    return out


def _share_price(state: dict) -> float:
    assets = _int(state, "total_supply_assets")
    shares = _int(state, "total_supply_shares")
    if shares <= 0:
        raise SettleError("the market has no supply shares; its price is undefined")
    return assets / shares


def realized_morpho(size_raw: int, entry: dict, exit_: dict,
                    entry_ts: int, exit_ts: int) -> dict:
    """What a supply of `size_raw` USDG earned, from the share price change."""
    a = accrue_morpho(entry, entry_ts)
    b = accrue_morpho(exit_, exit_ts)
    p0, p1 = _share_price(a), _share_price(b)
    gross_raw = round(size_raw * (p1 / p0 - 1))
    return {"gross_usdg": _usdg(gross_raw),
            "method": "morpho share price, accrued to each state's own block",
            "detail": {"entry_share_price": repr(p0), "exit_share_price": repr(p1),
                       "price_ratio": repr(p1 / p0),
                       "entry_accrued": a.get("accrued", False),
                       "exit_accrued": b.get("accrued", False),
                       "exit_accrued_seconds": b.get("accrued_seconds"),
                       "note": b.get("accrual_note") or a.get("accrual_note")}}


# --------------------------------------------------------------------------
# ERC-4626
# --------------------------------------------------------------------------

def realized_vault(size_raw: int, entry: dict, exit_: dict) -> dict:
    """What a vault deposit earned, from `convertToAssets` at both ends."""
    p0, p1 = _int(entry, "share_price"), _int(exit_, "share_price")
    if p0 <= 0:
        raise SettleError("the vault priced a share at zero at entry")
    gross_raw = round(size_raw * (p1 / p0 - 1))
    detail = {"entry_share_price": str(p0), "exit_share_price": str(p1),
              "price_ratio": repr(p1 / p0)}
    if p1 == p0:
        detail["note"] = ("the share price did not move by one unit over the hold; "
                          "six decimals of share price cannot resolve less")
    return {"gross_usdg": _usdg(gross_raw),
            "method": "erc-4626 share price from convertToAssets", "detail": detail}


# --------------------------------------------------------------------------
# Uniswap v3, full range
# --------------------------------------------------------------------------

def _position_liquidity(size_raw: int, sqrt_p: int, usdg_is_token0: bool) -> float:
    """The liquidity of a full-range position worth `size_raw` USDG at
    `sqrt_p`.

    A full-range position holds `L / sqrtP` of token0 and `L * sqrtP` of
    token1, so valued in token0 it is worth `2L / sqrtP` and valued in token1
    `2L * sqrtP`. Invert whichever of those USDG is.
    """
    s = sqrt_p / Q96
    if s <= 0:
        raise SettleError("the pool has a zero price; a position cannot be valued")
    return size_raw * s / 2 if usdg_is_token0 else size_raw / (2 * s)


def _position_value(liquidity: float, sqrt_p: int, usdg_is_token0: bool) -> float:
    s = sqrt_p / Q96
    if s <= 0:
        raise SettleError("the pool has a zero price; a position cannot be valued")
    return 2 * liquidity / s if usdg_is_token0 else 2 * liquidity * s


def realized_v3(size_raw: int, entry: dict, exit_: dict) -> dict:
    """What a full-range LP position earned: fees, less impermanent loss.

    Liquidity is pinned at entry so the position is worth exactly the
    allocation there. Revaluing that same liquidity at the exit price is what
    makes impermanent loss visible: a price that moved leaves the position
    worth less than the two tokens would have been worth held apart, and that
    shows here as a negative gross before a single unit of gas is charged.
    """
    is0 = str(entry.get("usdg_is_token0", exit_.get("usdg_is_token0"))).lower() in ("true", "1")
    s0, s1 = _int(entry, "sqrt_price_x96"), _int(exit_, "sqrt_price_x96")
    liq = _position_liquidity(size_raw, s0, is0)
    value_raw = _position_value(liq, s1, is0)

    # Fee growth counters are unsigned and wrap; the delta is taken modulo
    # 2**256 the way the pool's own accounting does.
    d0 = (_int(exit_, "fee_growth_0") - _int(entry, "fee_growth_0")) % UINT256
    d1 = (_int(exit_, "fee_growth_1") - _int(entry, "fee_growth_1")) % UINT256
    fees_raw0 = d0 / Q128 * liq
    fees_raw1 = d1 / Q128 * liq
    price = (s1 / Q96) ** 2                      # token1 raw per token0 raw
    if is0:
        fees_usdg_raw = fees_raw0 + (fees_raw1 / price if price else 0.0)
    else:
        fees_usdg_raw = fees_raw1 + fees_raw0 * price

    gross_raw = round(value_raw + fees_usdg_raw - size_raw)
    return {"gross_usdg": _usdg(gross_raw),
            "method": "full-range v3 position revalued at the exit price, plus its "
                      "share of feeGrowthGlobal",
            "detail": {"liquidity": repr(liq),
                       "entry_sqrt_price_x96": str(s0), "exit_sqrt_price_x96": str(s1),
                       "price_move_pct": repr(((s1 / s0) ** 2 - 1) * 100),
                       "value_at_exit_usdg": _usdg(round(value_raw)),
                       "impermanent_loss_usdg": _usdg(round(value_raw - size_raw)),
                       "fees_usdg": _usdg(round(fees_usdg_raw))}}


# --------------------------------------------------------------------------
# The dispatch
# --------------------------------------------------------------------------

def realized_gross(row: dict, exit_state: dict, exit_ts: int) -> dict:
    """The money the venue produced over the hold, before either leg's cost.

    Offline and pure: it takes the state recorded on the row at entry and the
    state read at exit, and does arithmetic. Every settlement in this repo
    goes through here, which is why the settlement test can replay a row
    against two recorded blocks and get the same answer the chain would give.
    """
    kind = row["kind"]
    if kind == "idle":
        return {"gross_usdg": _usdg(0),
                "method": "idle earns nothing and costs nothing to hold",
                "detail": {}}

    entry_state = row.get("entry_state") or {}
    if not entry_state:
        raise SettleError(
            f"{row['id']} has no entry state, so there is nothing to settle against. "
            "The endpoint drops state after about ten minutes, so it cannot be "
            "re-read; void the row instead of guessing")
    if not exit_state:
        raise SettleError(f"{row['id']}: the venue returned no state at exit")

    if kind in ledger.LIQUIDITY_CHECKED_KINDS:
        free = ledger.free_liquidity(kind, exit_state)
        need = _cfg()["min_free_liquidity_multiple"]
        size_usdg = ledger._dec(row["size_usdg"])
        if free is None:
            raise SettleError(f"{row['id']}: exit state lacks the fields free "
                              "liquidity is read from, so a withdraw cannot be "
                              "shown to succeed")
        if free < size_usdg * ledger._dec(need):
            sim = exit_state.get("exit_simulation")
            why = (f"a simulated withdraw of {ledger.free_liquidity_amount(sim)} USDG "
                   f"reverted for every holder tried ({'; '.join(sim.get('errors') or [sim.get('note', '')])})"
                   if isinstance(sim, dict) and not sim.get("ok") else
                   f"free liquidity at exit is {free} USDG, under "
                   f"min_free_liquidity_multiple {need} x {size_usdg}")
            raise Unexitable(row["id"], why)
    return _venue_gross(row, entry_state, exit_state, exit_ts)


def _venue_gross(row: dict, entry_state: dict, exit_state: dict, exit_ts: int) -> dict:
    """The kind's own arithmetic, with no exit check in front of it."""
    kind = row["kind"]
    size_raw = abi.str_to_fixed(row["size_usdg"], _cfg()["quote_asset_decimals"])
    if kind == "morpho_market":
        return realized_morpho(size_raw, entry_state, exit_state,
                               int(row["entry_ts"]), int(exit_ts))
    if kind == "erc4626_usdg_vault":
        return realized_vault(size_raw, entry_state, exit_state)
    if kind == "uniswap_v3_pool":
        return realized_v3(size_raw, entry_state, exit_state)
    raise SettleError(f"no settlement maths for venue kind {kind!r}")


def marked_gross(row: dict, exit_state: dict, exit_ts: int) -> dict:
    """The gross a loss mark books: the position valued at the exit state,
    capped at what the venue could pay a withdrawer at that block, less the
    size.

    What the venue could pay is `ledger.free_liquidity`, the same read the
    exit check refused on: total supply minus total borrow for a Morpho
    market, and for a vault the simulated withdraw, which proves nothing
    when it reverts, so the mark is zero. That can understate a vault that
    would pay part of the position, never overstate one, and the re-check
    corrects it.
    """
    full = _venue_gross(row, row["entry_state"], exit_state, exit_ts)
    size = ledger._dec(row["size_usdg"])
    value = size + ledger._dec(full["gross_usdg"])
    free = ledger.free_liquidity(row["kind"], exit_state) or ledger._dec(0)
    paid = max(min(value, free), ledger._dec(0))
    return {"gross_usdg": ledger.usdg_amount(paid - size), "paid_usdg": ledger.usdg_amount(paid),
            "method": f"loss mark: the position at the exit state, capped at what the "
                      f"venue could pay; valued by {full['method']}",
            "detail": dict(full["detail"], position_value_usdg=ledger.usdg_amount(value),
                           withdrawable_usdg=ledger.usdg_amount(free))}


# --------------------------------------------------------------------------
# Which rows are due
# --------------------------------------------------------------------------

def due(rows: list[dict], now_ts: float | None = None, cfg: dict | None = None) -> list[dict]:
    """Open rows whose forecast horizon has passed, and every marked row.

    The horizon on the row decides, and it was written before the outcome was
    known. `min_hold_minutes` is a floor underneath it, checked here and again
    in `core/ledger.py` at write time. A marked row is re-checked every run.
    """
    cfg = cfg or _cfg()
    now = time.time() if now_ts is None else now_ts
    floor = float(cfg["min_hold_minutes"])
    out = []
    for row in rows:
        if row.get("status") == "marked":
            out.append(row)
        elif row.get("status") == "open":
            held = (now - int(row["entry_ts"])) / 60
            if held >= max(float(row["forecast_horizon_min"]), floor):
                out.append(row)
    return out


# --------------------------------------------------------------------------
# Settling against the live chain
# --------------------------------------------------------------------------

def settle_row(row: dict, path: Path = ledger.LEDGER_PATH,
               dry_run: bool = False) -> dict:
    """Read the venue now, work out what the position made, and close the row.

    The exit is charged at this block's gas price, not at the price the entry
    saw. Gas on 4663 has moved twofold inside a day, so an exit priced from
    the entry's snapshot would quietly flatter every settled row.

    A row the venue cannot pay back is left open until
    `loss_mark_after_horizon_minutes` past its horizon, then marked. A marked
    row the venue still cannot pay back is reported as such and left alone.
    """
    # The holder whose withdraw passed at entry goes first: when it still
    # holds enough shares, its simulation decides without a depositor scan.
    sim = (row.get("entry_state") or {}).get("exit_simulation") or {}
    quote = ledger.live_quote(row["venue"], row.get("market_id"), size_usdg=row["size_usdg"],
                              holders=[sim["holder"]] if sim.get("holder") else [],
                              vault_rates=False)
    exit_state = quote.get("settle_state") or {}
    exit_block, exit_ts = int(quote["block"]), int(quote["ts"])

    mark_reason = None
    try:
        outcome = realized_gross(row, exit_state, exit_ts)
    except Unexitable as err:
        if row.get("status") == "marked":
            return {"id": row["id"], "venue": row["venue"], "kind": row["kind"],
                    "status": "marked", "recheck": f"still not exitable: {err.why}"}
        grace = float(_cfg()["loss_mark_after_horizon_minutes"])
        overdue = (exit_ts - int(row["entry_ts"])) / 60 - float(row["forecast_horizon_min"])
        if overdue < grace:
            raise
        outcome = marked_gross(row, exit_state, exit_ts)
        mark_reason = (f"not exitable {overdue / 60:.1f} h past the horizon, over "
                       f"loss_mark_after_horizon_minutes {grace:g}: {err.why}. Marked at "
                       f"{outcome['paid_usdg']} USDG, what the venue could pay; re-checked "
                       "every run and settled over the real hold when the venue pays it back")
    trip = cost.round_trip(row["kind"], float(row["size_usdg"]))
    # A mark that withdraws nothing sends no exit transaction.
    exit_cost = (trip["exit_cost_usdg"] if not mark_reason or float(outcome["paid_usdg"]) > 0
                 else "0.000000")

    if dry_run:
        hold = (exit_ts - int(row["entry_ts"])) / 60
        net = (float(outcome["gross_usdg"]) - float(exit_cost)
               - float(row["entry_cost_usdg"]))
        return {"id": row["id"], "venue": row["venue"], "kind": row["kind"],
                "dry_run": True, "exit_block": exit_block, "exit_ts": exit_ts,
                "hold_minutes": round(hold, 2),
                "realized_gross_usdg": outcome["gross_usdg"],
                "exit_cost_usdg": exit_cost,
                "realized_usdg": ledger.usdg_amount(net),
                "realized_net_apy": ledger.net_apy(row["size_usdg"],
                                                   ledger.usdg_amount(net), hold),
                "forecast_net_apy": row["forecast_net_apy"],
                "advertised_apy": row.get("advertised_apy"),
                "method": outcome["method"], "detail": outcome["detail"],
                "status": "marked" if mark_reason else "settled",
                "mark_reason": mark_reason}

    settled = ledger.settle(row["id"], exit_block, exit_ts, exit_cost,
                            outcome["gross_usdg"], exit_state=exit_state,
                            note=outcome["method"], path=path, mark_reason=mark_reason)
    settled["settle_detail"] = outcome["detail"]
    return settled


def run(row_id: str | None = None, path: Path = ledger.LEDGER_PATH,
        dry_run: bool = False, now_ts: float | None = None) -> list[dict]:
    """Settle every due row, or the one named. A row that cannot settle
    honestly is reported and left open for the operator, or marked once it is
    far enough past its horizon, never voided here: voiding a loser is how a
    ledger lies about itself."""
    rows = ledger.load(path)
    if row_id:
        wanted = [ledger.find(rows, row_id)]
        if wanted[0]["status"] not in ("open", "marked"):
            raise SettleError(f"{row_id} is {wanted[0]['status']}, not open or marked")
    else:
        wanted = due(rows, now_ts)

    out = []
    for row in wanted:
        try:
            out.append(settle_row(row, path, dry_run))
        except (SettleError, ledger.LedgerError, cost.CostError, venues.VenueError) as err:
            out.append({"id": row["id"], "venue": row["venue"], "kind": row["kind"],
                        "error": str(err)})
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _pct(value: object) -> str:
    return "      -" if value is None else f"{float(value) * 100:6.2f}%"


def _print(results: list[dict]) -> None:
    if not results:
        print("nothing due")
        return
    for r in results:
        if r.get("error"):
            print(f"{r['id']}  {r['kind']:20s} could not settle: {r['error']}")
            continue
        if r.get("recheck"):
            print(f"{r['id']}  {r['kind']:20s} marked, {r['recheck']}")
            continue
        print(f"{r['id']}  {r['kind']:20s} {r['venue'][:12]:12s} "
              f"held {r['hold_minutes'] / 60:6.1f}h  "
              f"gross {r['realized_gross_usdg']:>10s}  "
              f"exit {r['exit_cost_usdg']:>9s}  "
              f"net {r['realized_usdg']:>10s}  "
              f"realized {_pct(r['realized_net_apy'])} vs forecast "
              f"{_pct(r.get('forecast_net_apy'))}"
              + ("  MARKED" if r.get("status") == "marked" else ""))
        if r.get("detail"):
            print(f"         {r['method']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("due", help="open rows past their forecast horizon, and marked rows")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="settle due rows against live on-chain state")
    p.add_argument("--id", help="settle this row instead of everything due")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would settle, write nothing")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("explain", help="replay one row's maths offline against "
                                       "the state already on it")
    p.add_argument("id")

    args = ap.parse_args(argv)
    rows = ledger.load()

    if args.cmd == "due":
        pending = due(rows)
        if args.json:
            print(json.dumps(pending, indent=1, sort_keys=True))
        else:
            now = time.time()
            for r in pending:
                held = (now - int(r["entry_ts"])) / 60
                print(f"{r['id']}  {r['kind']:20s} {r['venue'][:12]:12s} "
                      f"held {held / 60:6.1f}h of a {r['forecast_horizon_min'] / 60:.1f}h horizon")
            marked = sum(1 for r in pending if r["status"] == "marked")
            print(f"{len(pending) - marked} of {len(ledger.open_rows(rows))} open rows "
                  f"are due, and {marked} marked rows are re-checked")
        return 0

    if args.cmd == "explain":
        try:
            row = ledger.find(rows, args.id)
        except ledger.LedgerError as err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        rpc.forbid_network("settle explain replays recorded state and reads nothing")
        state = row.get("exit_state") or row.get("entry_state") or {}
        ts = int(row.get("exit_ts") or row["entry_ts"])
        replay = marked_gross if row["status"] == "marked" else realized_gross
        print(json.dumps(replay(row, state, ts), indent=1, sort_keys=True))
        return 0

    if args.cmd == "run":
        try:
            results = run(args.id, dry_run=args.dry_run)
        except (SettleError, ledger.LedgerError) as err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(results, indent=1, sort_keys=True))
        else:
            _print(results)
        print(f"rpc {json.dumps(rpc.stats())}", file=sys.stderr)
        return 1 if any(r.get("error") for r in results) else 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
