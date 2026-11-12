"""What a move costs, priced from live chain state.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

Owns the two costs an allocation actually pays:

  - **Gas.** Charged at the block's own gas price, converted to USDG through
    the deepest USDG pool of the gas token. Gas *units* per action come from
    the table below, which was measured on 4663; gas *price* is read live every
    time, because the recon memo's verdict turns on it.
  - **Swap price impact.** Charged against the live liquidity and tick of the
    pool the swap would actually route through, fee included. At 250 USDG
    against a 33M pool this is under a hundredth of a basis point, so on this
    chain the cost of a move is gas and very nearly only gas. The model is here
    anyway, because that is a fact about today's depth and not a law.

A move that costs more than it earns must show as a loss. This module never
rounds a cost down to zero and never nets a cost against a rate it was not
given.

It owns no judgment. It does not decide whether a cost is acceptable, does not
know `min_expected_net_bps`, and does not refuse anything. It answers "what
does this cost" and "what must this earn to break even"; what to do about the
answer is `strategy/`, and enforcement is `core/ledger.py`.

Commands:
    python3 core/cost.py                      # the round-trip table, live
    python3 core/cost.py --size 250 --json
    python3 core/cost.py --apy 0.0504 --kind morpho_market --hold-days 3
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import abi, rpc, venues

SECONDS_YEAR = 365 * 24 * 3600
Q96 = 2 ** 96

# Gas units per action, measured on chain 4663 on 2026-09-08. Each entry says
# where the number came from, because they are not all equally strong: an
# `eth_estimateGas` from an address that already holds the token and has a live
# allowance is the real cost of the real call, while a receipt median also
# carries whatever else that transaction did.
#
# Real users on 4663 transact through ERC-4337, and an EntryPoint UserOp costs
# about 475,000 gas whatever it wraps, so a receipt taken at random measures
# the smart-account bundle rather than the action. Receipt-derived rows below
# are filtered by log count for that reason, and the one row that could not be
# filtered is marked as an upper bound.
GAS_UNITS: dict[str, tuple[int, str]] = {
    "approve":          (41_260,  "eth_estimateGas of approve(USDG) from a live holder"),
    "morpho_supply":    (122_826, "eth_estimateGas of supply(250 USDG) from a live approver"),
    "morpho_withdraw":  (117_593, "eth_estimateGas of withdraw from a live supplier"),
    "erc4626_deposit":  (474_921, "upper bound: the full EntryPoint UserOp that performs it"),
    "erc4626_redeem":   (376_994, "eth_estimateGas of redeem from a live shareholder"),
    "v3_swap":          (123_283, "median of 3 receipts with 4 or fewer logs"),
    "v3_mint":          (371_704, "median of 18 receipts"),
    "v3_burn":          (204_804, "median of 27 receipts"),
}

# Steps in a round trip, split at the point the ledger splits: what an entry
# pays and what an exit pays. `swap_legs` is how many swaps each leg performs;
# an LP entry converts half the allocation into the other token and the exit
# converts it back, so each leg swaps half the position.
ROUND_TRIPS: dict[str, dict] = {
    "morpho_market": {
        "entry": ("approve", "morpho_supply"),
        "exit": ("morpho_withdraw",),
        "swap_legs": 0, "swap_fraction": 0.0},
    "erc4626_usdg_vault": {
        "entry": ("approve", "erc4626_deposit"),
        "exit": ("erc4626_redeem",),
        "swap_legs": 0, "swap_fraction": 0.0},
    "uniswap_v3_pool": {
        "entry": ("approve", "approve", "v3_swap", "v3_mint"),
        "exit": ("v3_burn", "v3_swap"),
        "swap_legs": 1, "swap_fraction": 0.5},
    "idle": {"entry": (), "exit": (), "swap_legs": 0, "swap_fraction": 0.0},
}


class CostError(Exception):
    """A move cannot be priced from what has been measured on this chain."""


def _cfg() -> dict:
    return rpc.protected_config()


def _usdg(amount: float) -> str:
    """A cost, as the repo's canonical decimal string. Costs round up, so the
    engine never understates what a move takes."""
    dec = _cfg()["quote_asset_decimals"]
    return abi.fixed_to_str(math.ceil(amount * 10 ** dec - 1e-9), dec)


def _usdg_amount(amount: float) -> str:
    """A gain or a balance, rounded to nearest. Rounding a gain up would be the
    same mistake as rounding a cost down."""
    dec = _cfg()["quote_asset_decimals"]
    return abi.fixed_to_str(round(amount * 10 ** dec), dec)


# --------------------------------------------------------------------------
# Live inputs
# --------------------------------------------------------------------------

def gas_snapshot(block: int | None = None) -> dict:
    """Gas price at a block, and what one unit of gas costs in USDG there.

    The conversion runs through the deepest USDG pool of the chain's gas token,
    read live. Nothing here is cached across cycles: the recon memo's go
    verdict flips if gas rises tenfold, so this is read every time it is used.
    """
    if block is None:
        block = rpc.block_number() - 2
    head = rpc.get_block(block)
    price_wei = rpc.gas_price()
    pool = venues.reference_pool(block)
    if pool["other_decimals"] != 18:
        raise CostError(
            f"reference pool {pool['address']} prices a {pool['other_decimals']}-decimal "
            "token; gas is denominated in an 18-decimal gas token")
    return {"block": block,
            "ts": rpc.to_int(head["timestamp"]),
            "gas_price_wei": price_wei,
            "gas_price_gwei": price_wei / 1e9,
            "base_fee_wei": rpc.to_int(head.get("baseFeePerGas", "0x0")),
            "eth_usdg": pool["eth_usdg"],
            "usdg_per_gas": price_wei / 1e18 * pool["eth_usdg"],
            "reference_pool": pool["address"]}


def gas_cost_usdg(steps, snapshot: dict) -> tuple[int, float]:
    """Gas units and USDG for a list of actions."""
    units = 0
    for step in steps:
        if step not in GAS_UNITS:
            raise CostError(f"no measured gas for action {step!r} on chain 4663")
        units += GAS_UNITS[step][0]
    return units, units * snapshot["usdg_per_gas"]


# --------------------------------------------------------------------------
# Price impact
# --------------------------------------------------------------------------

def swap_impact(size_usdg: float, pool: dict) -> dict:
    """Cost of swapping `size_usdg` of USDG into the pool's other token.

    Constant-liquidity maths inside the current tick range, against the live
    `sqrtPriceX96` and `liquidity`. `ticks` is reported so a caller can see
    whether the trade stayed in range; a trade that moves more than a tick
    spacing has left the liquidity this model assumes and the number below it
    is a floor, not an estimate.
    """
    if size_usdg <= 0:
        return {"size_usdg": 0.0, "fee_bps": 0.0, "slippage_bps": 0.0,
                "all_in_bps": 0.0, "ticks": 0.0, "cost_usdg": 0.0}
    liq, sqrt_p = pool["liquidity"], pool["sqrt_price_x96"]
    if liq <= 0:
        raise CostError(f"pool {pool['address']} has no in-range liquidity")
    fee_ppm = pool["fee_ppm"]
    amount = int(size_usdg * 10 ** _cfg()["quote_asset_decimals"])
    net = amount * (10 ** 6 - fee_ppm) // 10 ** 6

    price_raw = (sqrt_p / Q96) ** 2                    # token1 raw per token0 raw
    if pool["usdg_is_token0"]:
        # Selling token0 into the pool moves sqrtP down.
        sp_new = (liq << 96) * sqrt_p // ((liq << 96) + net * sqrt_p)
        out = liq * (sqrt_p - sp_new) >> 96            # token1 raw out
        mid = 1 / price_raw if price_raw else 0.0      # USDG raw per other raw
    else:
        # Selling token1 into the pool moves sqrtP up.
        sp_new = sqrt_p + (net << 96) // liq
        out = liq * ((1 << 96) * (sp_new - sqrt_p)) // (sqrt_p * sp_new)   # token0 raw out
        mid = price_raw
    if out <= 0 or mid <= 0:
        raise CostError(f"pool {pool['address']} returns nothing for {size_usdg} USDG")

    executed = amount / out                            # USDG raw per other raw
    all_in_bps = (executed / mid - 1) * 10_000
    ticks = math.log((sp_new / sqrt_p) ** 2) / math.log(1.0001)
    return {"size_usdg": size_usdg,
            "fee_bps": fee_ppm / 100,
            "slippage_bps": all_in_bps - fee_ppm / 100,
            "all_in_bps": all_in_bps,
            "ticks": ticks,
            "cost_usdg": size_usdg * all_in_bps / 10_000}


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------

def round_trip(kind: str, size_usdg: float, snapshot: dict | None = None,
               pool: dict | None = None) -> dict:
    """Entry cost, exit cost, and the total, for one allocation of `size_usdg`.

    Costs are in USDG and are close to fixed, so the same round trip is five
    times as expensive in basis points at a fifth of the size. That is the
    single most important thing this function says.
    """
    if kind not in ROUND_TRIPS:
        raise CostError(
            f"no measured round trip for venue kind {kind!r}. Measured kinds: "
            + ", ".join(sorted(ROUND_TRIPS)))
    trip = ROUND_TRIPS[kind]
    snapshot = snapshot or gas_snapshot()

    entry_gas, entry_usdg = gas_cost_usdg(trip["entry"], snapshot)
    exit_gas, exit_usdg = gas_cost_usdg(trip["exit"], snapshot)

    entry_swap = exit_swap = 0.0
    impact = None
    if trip["swap_legs"]:
        pool = pool or venues.reference_pool(snapshot["block"])
        impact = swap_impact(size_usdg * trip["swap_fraction"], pool)
        entry_swap = impact["cost_usdg"] * trip["swap_legs"]
        # The exit swaps the other side back. Its value at exit is unknown at
        # entry; charging the same notional is the honest symmetric estimate.
        exit_swap = entry_swap

    total = entry_usdg + entry_swap + exit_usdg + exit_swap
    return {"kind": kind,
            "size_usdg": _usdg_amount(size_usdg),
            "block": snapshot["block"],
            "gas_price_gwei": snapshot["gas_price_gwei"],
            "eth_usdg": snapshot["eth_usdg"],
            "entry_steps": list(trip["entry"]),
            "exit_steps": list(trip["exit"]),
            "entry_gas": entry_gas,
            "exit_gas": exit_gas,
            "entry_cost_usdg": _usdg(entry_usdg + entry_swap),
            "exit_cost_usdg": _usdg(exit_usdg + exit_swap),
            "total_cost_usdg": _usdg(total),
            "swap_cost_usdg": _usdg(entry_swap + exit_swap),
            "bps": (total / size_usdg * 10_000) if size_usdg else 0.0,
            "impact": impact}


def hurdle_apy(cost_usdg: float, size_usdg: float, hold_minutes: float) -> float:
    """The annualized rate a position must earn over `hold_minutes` just to
    pay for the round trip. Below this the allocation loses money however
    attractive the advertised number is."""
    if size_usdg <= 0 or hold_minutes <= 0:
        return float("inf")
    return (1 + cost_usdg / size_usdg) ** (SECONDS_YEAR / (hold_minutes * 60)) - 1


def net_of_cost(gross_apy: float, size_usdg: float, hold_minutes: float,
                cost_usdg: float) -> dict:
    """What an allocation nets after its round trip.

    `gross_apy` is a rate, not an outcome. Feeding an advertised rate in here
    produces a forecast; feeding a realized rate in produces a settled result.
    The caller keeps the two apart, this function cannot.

    The prorate is compounding-consistent: a hold of `t` years at `apy` earns
    `(1 + apy) ** t - 1`, not `apy * t`. Every rate this engine reads is
    already an effective annual rate - Morpho's comes from `expm1` of the
    per-second rate - so simple interest would overstate a seven-day hold at
    63% by about a third.
    """
    years = hold_minutes * 60 / SECONDS_YEAR
    gross_usdg = size_usdg * ((1 + gross_apy) ** years - 1)
    net_usdg = gross_usdg - cost_usdg
    ratio = 1 + net_usdg / size_usdg if size_usdg else 0.0
    net_apy = (ratio ** (1 / years) - 1) if (ratio > 0 and years > 0) else float("-inf")
    return {"gross_apy": gross_apy,
            "hold_minutes": hold_minutes,
            "gross_usdg": _usdg_amount(gross_usdg),
            "cost_usdg": _usdg(cost_usdg),
            "net_usdg": _usdg_amount(net_usdg),
            "net_bps": (net_usdg / size_usdg * 10_000) if size_usdg else 0.0,
            "net_apy": net_apy,
            "hurdle_apy": hurdle_apy(cost_usdg, size_usdg, hold_minutes)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HOLD_DAYS = (1, 3, 7)


def report(sizes: list[float]) -> dict:
    snapshot = gas_snapshot()
    pool = venues.reference_pool(snapshot["block"])
    out = {"snapshot": snapshot,
           "reference_pool": {k: pool[k] for k in
                              ("address", "fee_ppm", "tick", "liquidity",
                               "usdg_is_token0", "eth_usdg")},
           "sizes": [], "generated_at": int(time.time())}
    for size in sizes:
        entry = {"size_usdg": _usdg(size), "round_trips": [], "impact": None}
        try:
            entry["impact"] = swap_impact(size, pool)
        except CostError as exc:
            entry["impact_error"] = str(exc)
        for kind in ROUND_TRIPS:
            rt = round_trip(kind, size, snapshot, pool)
            rt["hurdle_apy"] = {
                f"{d}d": hurdle_apy(float(rt["total_cost_usdg"]), size, d * 24 * 60)
                for d in HOLD_DAYS}
            rt["hurdle_apy"][f"{_cfg()['min_hold_minutes']}min"] = hurdle_apy(
                float(rt["total_cost_usdg"]), size, _cfg()["min_hold_minutes"])
            entry["round_trips"].append(rt)
        out["sizes"].append(entry)
    return out


def _fmt_hurdle(apy: float) -> str:
    """A break-even rate, readably. Annualizing a fixed cost over a short hold
    produces true but unreadable numbers - the round trip on a one-hour Morpho
    hold breaks even at about 29,000% APY - so anything past four figures is
    shown as a ceiling. The point it makes is the same: no rate on this chain
    clears it."""
    if apy == float("inf"):
        return "     n/a"
    if apy >= 10:
        return "  >999%"
    return f"{apy:7.1%}"


def _print_report(data: dict) -> None:
    s = data["snapshot"]
    p = data["reference_pool"]
    print(f"block {s['block']:,}  gas {s['gas_price_gwei']:.4f} gwei  "
          f"gas token {s['eth_usdg']:,.2f} USDG  "
          f"1 gas = {s['usdg_per_gas']:.9f} USDG")
    print(f"reference pool {p['address']} fee {p['fee_ppm']/10_000:.2f}% "
          f"tick {p['tick']:,} L {p['liquidity']:,}")

    min_hold = f"{_cfg()['min_hold_minutes']}min"
    for entry in data["sizes"]:
        size = float(entry["size_usdg"])
        i = entry.get("impact")
        head = f"\n== {size:,.2f} USDG"
        if i:
            head += (f"   swap fee {i['fee_bps']:.2f} bps, slippage "
                     f"{i['slippage_bps']:.4f} bps, {i['ticks']:.4f} ticks")
        print(head)
        print(f"{'round trip':20s} {'gas':>9} {'entry':>8} {'exit':>8} {'total':>8} "
              f"{'bps':>6}   {'be ' + min_hold:>7} {'be 1d':>7} {'be 3d':>7} {'be 7d':>7}")
        for rt in entry["round_trips"]:
            h = rt["hurdle_apy"]
            print(f"{rt['kind']:20s} {rt['entry_gas'] + rt['exit_gas']:>9,} "
                  f"{float(rt['entry_cost_usdg']):>8.4f} {float(rt['exit_cost_usdg']):>8.4f} "
                  f"{float(rt['total_cost_usdg']):>8.4f} {rt['bps']:>6.1f}   "
                  f"{_fmt_hurdle(h[min_hold])} {_fmt_hurdle(h['1d'])} "
                  f"{_fmt_hurdle(h['3d'])} {_fmt_hurdle(h['7d'])}")
    print("\nbe = the APY a position must earn over that hold just to pay for the "
          "round trip.")


def self_test() -> None:
    """Check the maths offline, against a pool state fixed here rather than read.

    The pool below is the deepest USDG/WETH pool as it stood on 2026-09-09,
    read with `venues.reference_pool`. Its numbers are frozen so this check
    means the same thing every time it runs, and so it runs where there is no
    endpoint.
    """
    pool = {"address": "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca",
            "liquidity": 11_174_923_106_290_809_094,
            "sqrt_price_x96": 3_949_135_289_820_384_151_731_381,
            "tick": -198_142,
            "fee_ppm": 100, "usdg_is_token0": False, "other_decimals": 18}

    small, large = swap_impact(50, pool), swap_impact(250, pool)
    assert small["fee_bps"] == 1.0, small
    # Slippage is a cost, so it is positive, tiny at this depth, and rises with
    # size roughly in proportion.
    assert 0 < small["slippage_bps"] < large["slippage_bps"] < 0.01, (small, large)
    assert large["ticks"] > 0, large
    assert abs(large["cost_usdg"] / small["cost_usdg"] - 5) < 0.05, (small, large)
    assert swap_impact(0, pool)["cost_usdg"] == 0.0

    # A fixed cost is a fixed number of USDG, so it is five times as many basis
    # points at a fifth of the size. This is the model's central claim.
    snapshot = {"block": 0, "usdg_per_gas": 5.81e-7, "gas_price_gwei": 0.234,
                "eth_usdg": 2484.15}
    big = round_trip("morpho_market", 250, snapshot, pool)
    lil = round_trip("morpho_market", 50, snapshot, pool)
    assert big["total_cost_usdg"] == lil["total_cost_usdg"], (big, lil)
    assert abs(lil["bps"] / big["bps"] - 5) < 1e-9, (big, lil)
    assert big["entry_gas"] == 41_260 + 122_826 and big["exit_gas"] == 117_593

    idle = round_trip("idle", 250, snapshot, pool)
    assert idle["total_cost_usdg"] == "0.000000" and idle["bps"] == 0.0

    # An LP round trip charges swap impact; a supply round trip does not.
    lp = round_trip("uniswap_v3_pool", 250, snapshot, pool)
    assert float(lp["swap_cost_usdg"]) > 0 and big["swap_cost_usdg"] == "0.000000"

    try:
        round_trip("uniswap_v2_pool", 250, snapshot, pool)
        raise AssertionError("a kind with no measured gas must not be priced")
    except CostError:
        pass

    # Gas above yield must read as a loss, not as a small gain.
    cost = float(big["total_cost_usdg"])
    hour = net_of_cost(0.6372, 250, 60, cost)
    assert float(hour["net_usdg"]) < 0 and hour["net_bps"] < 0, hour
    assert abs(hour["net_bps"] + 5.9) < 0.2, hour
    # A week at the same rate clears easily. The prorate is compounding-
    # consistent, so this is 88 bps and not the 121 bps simple interest would
    # give: the rates these venues expose are already effective annual rates.
    week = net_of_cost(0.6372, 250, 7 * 24 * 60, cost)
    assert 85 < week["net_bps"] < 95, week

    # Break-even is the rate at which net is zero, to within rounding.
    for hold in (60, 1440, 7 * 1440):
        h = hurdle_apy(cost, 250, hold)
        assert abs(net_of_cost(h, 250, hold, cost)["net_bps"]) < 0.01, (hold, h)
    assert hurdle_apy(cost, 0, 60) == float("inf")
    print("core/cost.py self-test: ok")


def main(argv: list[str] | None = None) -> int:
    cfg = _cfg()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--size", type=float, action="append",
                    help="allocation size in USDG; repeatable. Default: "
                         "max_allocation_usdg and one fifth of it")
    ap.add_argument("--kind", default=None, help="price one venue kind only")
    ap.add_argument("--apy", type=float, default=None,
                    help="net a gross rate against the round trip")
    ap.add_argument("--hold-days", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="check the maths offline, without the endpoint")
    args = ap.parse_args(argv)

    if args.selftest:
        self_test()
        return 0

    sizes = args.size or [cfg["max_allocation_usdg"], cfg["max_allocation_usdg"] / 5]

    if args.apy is not None:
        kind = args.kind or "morpho_market"
        hold_min = (args.hold_days * 24 * 60) if args.hold_days else cfg["min_hold_minutes"]
        snapshot = gas_snapshot()
        out = []
        for size in sizes:
            rt = round_trip(kind, size, snapshot)
            net = net_of_cost(args.apy, size, hold_min, float(rt["total_cost_usdg"]))
            out.append({"size_usdg": rt["size_usdg"], "round_trip": rt, "net": net})
        if args.json:
            print(json.dumps(out, indent=1, sort_keys=True))
        else:
            print(f"{kind} at {args.apy:.2%} gross, held {hold_min:,.0f} min "
                  f"({hold_min/1440:.2f} d)")
            print(f"{'size':>10} {'cost':>9} {'gross':>10} {'net USDG':>10} "
                  f"{'net bps':>9} {'net APY':>9} {'hurdle APY':>11}")
            for r in out:
                n = r["net"]
                print(f"{float(r['size_usdg']):>10,.2f} {float(n['cost_usdg']):>9.4f} "
                      f"{float(n['gross_usdg']):>10.4f} {float(n['net_usdg']):>10.4f} "
                      f"{n['net_bps']:>9.1f} {n['net_apy']:>9.2%} "
                      f"{_fmt_hurdle(n['hurdle_apy']):>11}")
        return 0

    data = report(sizes)
    if args.kind:
        for entry in data["sizes"]:
            entry["round_trips"] = [r for r in entry["round_trips"] if r["kind"] == args.kind]
    if args.json:
        print(json.dumps(data, indent=1, sort_keys=True))
    else:
        _print_report(data)
        print(f"rpc {json.dumps(rpc.stats())}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # The counters are the diagnosis when a run dies: throttling shows here
        # (cycle 74 printed nothing and could not tell).
        print(f"rpc {json.dumps(rpc.stats())}", file=sys.stderr)
        raise
