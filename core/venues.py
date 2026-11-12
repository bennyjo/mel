"""What is deployed on chain 4663, and what its live state says it pays.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

Owns:
  - `journal/venues.jsonl`, the record of every confirmed venue: address, kind,
    the block it was confirmed at, and how it was confirmed. This module is its
    only writer;
  - discovery, which extends that record from chain events, and refresh, which
    re-reads the live fields on rows already in it;
  - the quote: live depth and the venue's own advertised rate, read from state
    at a named block.

It owns no judgment. It does not rank venues, does not apply a depth floor,
does not decide what is dust, and does not know what an allocation is. Which
venues are worth using is `strategy/venue-filters.json`, which is the agent's.

An advertised rate is what the venue's own state implies before any Mel cost.
It is never a realized return and must never be written into a realized field.

Commands:
    python3 core/venues.py list [--kind KIND]
    python3 core/venues.py quote [--kind KIND] [--venue ADDR|MARKET_ID]
                                 [--top N] [--rank-by depth|rate]
                                 [--min-depth USDG] [--json]

`--top N` picks N recorded rows to quote live. `--rank-by depth` (default)
takes the deepest; `--rank-by rate` takes the highest advertised rate at the
last refresh. On 4663 depth and rate pull in opposite directions, so the
deepest rows are the worst-paying ones. `--min-depth` is applied to the
record before the pick, so a rate ranking never spends its budget on dust.
A quote also records a baseline forecast row in `journal/forecasts.jsonl`
for every quoted venue without a live one (`core/forecast.py`), so each
venue a cycle looks at is measured whether or not anyone allocates to it.
`--no-record` skips that, for an operator's ad-hoc read.
    python3 core/venues.py probe --venue ADDR|MARKET_ID [--size USDG]
    python3 core/venues.py refresh [--min-depth USDG]

`probe` simulates what the ledger cannot: for a vault, `deposit` and
`withdraw` by `eth_call` from a throwaway address, and the vault's own USDG
balance. A vault whose views answer but whose deposit reverts before the
token transfer is not a venue (a00006). Revert data is printed verbatim.
    python3 core/venues.py discover [--from-block N]
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

from core import abi, rpc

VENUES_PATH = ROOT / "journal" / "venues.jsonl"

SECONDS_YEAR = 365 * 24 * 3600
WAD = 10 ** 18
Q96 = 2 ** 96
Q128 = 2 ** 128

# The endpoint serves state for about 6,100 blocks (CHAIN.md). A trailing rate
# needs two reads inside that window; 4,000 blocks is about seven minutes and
# leaves room for the head to move while the batch is in flight.
RATE_WINDOW_BLOCKS = 4_000

# Fee tiers the v3 factories on 4663 answer `feeAmountTickSpacing` for.
V3_FEE_TIERS = (100, 500, 3000, 10000)

# An ERC-4626 rate cannot be read from state on this endpoint: share prices
# carry six decimals and state survives ten minutes, so the smallest visible
# move over a served window is about 4% APY and the usual reading is exactly
# zero. It comes from the vault's own events instead, which are retained to
# genesis. About a week at the ~857,000 blocks a day the chain currently runs.
VAULT_RATE_LOOKBACK_BLOCKS = 6_000_000
VAULT_RATE_MIN_ASSETS_USDG = 1_000     # below this the assets integer in the
                                       # event carries too few digits to price
VAULT_RATE_MIN_SECONDS = 3_600

BORROW_RATE_VIEW = ("borrowRateView((address,address,address,address,uint256),"
                    "(uint128,uint128,uint128,uint128,uint128,uint128))")

QUOTABLE = ("morpho_market", "erc4626_usdg_vault", "uniswap_v3_pool",
            "uniswap_v2_pool", "idle")

IDLE = {"address": "idle", "kind": "idle"}


class VenueError(Exception):
    """A venue cannot be quoted from the state that was read."""


def _cfg() -> dict:
    return rpc.protected_config()


def usdg_str(raw: int) -> str:
    return abi.fixed_to_str(int(raw), _cfg()["quote_asset_decimals"])


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------

def load(path: Path = VENUES_PATH) -> list[dict]:
    """Every recorded venue, in file order."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _key(row: dict) -> str:
    """The identity of a venue row. A Morpho instance holds many markets, so a
    market is identified by its id and everything else by its address."""
    if row.get("kind") == "morpho_market":
        return f"{row['address'].lower()}:{row['market_id'].lower()}"
    return row["address"].lower()


def save(rows: list[dict], path: Path = VENUES_PATH) -> None:
    """Rewrite the record. Rows are sorted by kind then key so a diff is
    readable, and each row's own keys are sorted so a refresh shows only the
    fields that actually moved."""
    rows = sorted(rows, key=lambda r: (r.get("kind", ""), _key(r)))
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def merge(existing: list[dict], found: list[dict]) -> tuple[list[dict], int]:
    """Fold `found` into `existing` by key. Returns the rows and how many were
    new. A rediscovered venue keeps its original `confirmed_block`: that is the
    evidence it was first seen at and it is not overwritten."""
    by_key = {_key(r): dict(r) for r in existing}
    new = 0
    for row in found:
        k = _key(row)
        if k in by_key:
            keep = by_key[k].get("confirmed_block")
            by_key[k].update(row)
            if keep is not None:
                by_key[k]["confirmed_block"] = keep
        else:
            by_key[k] = dict(row)
            new += 1
    return list(by_key.values()), new


def select(rows: list[dict], kind: str | None = None,
           venue: str | None = None) -> list[dict]:
    """Filter the record by kind, or to a single venue named by address or by
    market id (a unique prefix of either is enough)."""
    out = rows
    if kind:
        out = [r for r in out if r.get("kind") == kind]
    if venue:
        v = venue.lower()
        out = [r for r in out
               if r["address"].lower().startswith(v)
               or str(r.get("market_id", "")).lower().startswith(v)]
    return out


def recorded_depth(row: dict) -> float:
    """Depth in USDG as recorded at the last refresh, zero when unknown."""
    return float(row.get("depth_usdg") or row.get("total_assets_usdg") or 0)


def rank(rows: list[dict], by: str = "depth", top: int = 0,
         min_depth: float = 0.0) -> list[dict]:
    """The recorded rows worth quoting live, in the order a cycle should
    spend its quote budget. `min_depth` is applied before `top`, so the pick
    is the best N rows that pass the floor, not the best N rows minus those
    that fail it. Pure: nothing here touches the chain."""
    keys = {
        "depth": lambda r: -recorded_depth(r),
        "rate": lambda r: (-float(r.get("advertised_apy") or 0), -recorded_depth(r)),
    }
    if by not in keys:
        raise ValueError(f"rank by {by!r}: expected one of {sorted(keys)}")
    out = [r for r in rows if recorded_depth(r) >= min_depth]
    out = sorted(out, key=keys[by])
    return out[:top] if top else out


# --------------------------------------------------------------------------
# Live state: the reference pool
# --------------------------------------------------------------------------

_reference_cache: tuple[int, dict] | None = None


def reference_pool(block: int | str = "latest", max_age_blocks: int = 60) -> dict:
    """The deepest USDG-quoted Uniswap v3 pool, with the live state needed to
    price both ETH and a swap against it.

    Deepest is decided from balances read now, not from the balance recorded
    when the pool was confirmed, because depth moves. Everything downstream
    that converts gas to USDG goes through this one pool.
    """
    global _reference_cache
    if isinstance(block, int) and _reference_cache is not None:
        seen_at, cached = _reference_cache
        if abs(block - seen_at) <= max_age_blocks:
            return cached

    usdg = _cfg()["quote_asset_address"]
    pools = [r for r in load() if r.get("kind") == "uniswap_v3_pool"]
    if not pools:
        raise VenueError("no uniswap_v3_pool in journal/venues.jsonl; run discover")

    calls = [(usdg, abi.encode_call("balanceOf(address)", p["address"])) for p in pools]
    balances = rpc.eth_call_batch(calls, block=block, allow_errors=True)
    ranked = sorted(
        ((abi.decode_uint(b) if isinstance(b, str) and len(b) >= 66 else 0, p)
         for b, p in zip(balances, pools)),
        key=lambda t: -t[0])
    usdg_raw, pool = ranked[0]
    if usdg_raw == 0:
        raise VenueError("every recorded USDG/WETH pool reads a zero USDG balance")

    state = _pool_state([pool], block)[pool["address"].lower()]
    state["usdg_raw"] = usdg_raw
    if not state.get("eth_usdg"):
        # Say whether the RPC answered with an error or the pool has no priceable
        # other side; a retry clears the first and nothing clears the second.
        why = state.get("error") or "no RPC error recorded; slot0 or the pair is unpriceable"
        raise VenueError(f"cannot price the other side of {pool['address']}: {why}")
    if isinstance(block, int):
        _reference_cache = (block, state)
    return state


def _pool_state(pools: list[dict], block: int | str) -> dict[str, dict]:
    """slot0, liquidity, fee growth, token identity and both balances for each
    v3 pool, read in one batch."""
    usdg = _cfg()["quote_asset_address"].lower()
    calls, index = [], []
    for p in pools:
        a = p["address"]
        for sig in ("slot0()", "liquidity()", "feeGrowthGlobal0X128()",
                    "feeGrowthGlobal1X128()", "token0()", "token1()", "fee()"):
            calls.append((a, abi.encode_call(sig)))
            index.append((a.lower(), sig))
    outs = rpc.eth_call_batch(calls, block=block, allow_errors=True)
    raw: dict[str, dict] = {}
    for (addr, sig), out in zip(index, outs):
        raw.setdefault(addr, {})[sig] = out

    # A second batch: the non-USDG token's balance and decimals, which are only
    # knowable once token0/token1 are back.
    follow, findex = [], []
    for p in pools:
        a = p["address"].lower()
        r = raw[a]
        try:
            t0 = abi.decode_address(r["token0()"]).lower()
            t1 = abi.decode_address(r["token1()"]).lower()
        except Exception:
            continue
        other = t1 if t0 == usdg else t0
        r["_other"] = other
        follow += [(other, abi.encode_call("balanceOf(address)", p["address"])),
                   (other, abi.encode_call("decimals()")),
                   (_cfg()["quote_asset_address"],
                    abi.encode_call("balanceOf(address)", p["address"]))]
        findex += [(a, "other_balance"), (a, "other_decimals"), (a, "usdg_balance")]
    outs = rpc.eth_call_batch(follow, block=block, allow_errors=True)
    for (addr, key), out in zip(findex, outs):
        raw[addr][key] = out

    state: dict[str, dict] = {}
    for p in pools:
        a = p["address"].lower()
        r = raw[a]
        s = {"address": p["address"], "kind": "uniswap_v3_pool", "block": block}
        try:
            s["sqrt_price_x96"] = abi.decode_uint(r["slot0()"], 0)
            s["tick"] = abi.decode_int(r["slot0()"], 1, 24)
            s["liquidity"] = abi.decode_uint(r["liquidity()"])
            s["fee_growth_0"] = abi.decode_uint(r["feeGrowthGlobal0X128()"])
            s["fee_growth_1"] = abi.decode_uint(r["feeGrowthGlobal1X128()"])
            s["fee_ppm"] = abi.decode_uint(r["fee()"])
            t0 = abi.decode_address(r["token0()"])
            s["token0"], s["token1"] = t0, abi.decode_address(r["token1()"])
            s["usdg_is_token0"] = t0.lower() == usdg
            s["usdg_raw"] = abi.decode_uint(r["usdg_balance"])
            s["other_raw"] = abi.decode_uint(r["other_balance"])
            s["other_decimals"] = abi.decode_uint(r["other_decimals"])
            s["other_token"] = r["_other"]
            s["eth_usdg"] = _price_from_sqrt(
                s["sqrt_price_x96"], s["usdg_is_token0"],
                _cfg()["quote_asset_decimals"], s["other_decimals"])
        except Exception as exc:                       # noqa: BLE001 - recorded, not raised
            s["error"] = f"{type(exc).__name__}: {exc}"[:140]
        state[a] = s
    return state


def _price_from_sqrt(sqrt_price_x96: int, usdg_is_token0: bool,
                     usdg_decimals: int, other_decimals: int) -> float:
    """USDG per whole unit of the pool's other token."""
    raw = (sqrt_price_x96 / Q96) ** 2          # token1 raw per token0 raw
    if usdg_is_token0:
        raw = 1 / raw if raw else 0.0          # -> USDG raw per other raw
    return raw * 10 ** (other_decimals - usdg_decimals)


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------

def quote(rows: list[dict], window_blocks: int = RATE_WINDOW_BLOCKS,
          include_idle: bool = True, exit_usdg: float | None = None,
          exit_holders: dict[str, list[str]] | None = None,
          vault_rates: bool = True) -> list[dict]:
    """Live depth and advertised rate for each venue in `rows`.

    `exit_usdg`, when given, simulates a withdraw of that many USDG from each
    vault and records the outcome in `settle_state["exit_simulation"]`. It is
    a log scan and up to three calls per vault, so single-venue quotes (open
    and settle) ask for it and board quotes do not. `exit_holders` maps a
    vault address to holders worth trying before that scan; see
    `exit_simulation`.

    `vault_rates=False` skips the vault rate scan, another seven days of
    logs. Settlement reads only `settle_state`, never the advertised rate.

    Every quote carries `settle_state`: the on-chain numbers settlement will
    need to compute a realized return later. It is recorded because it cannot
    be re-read - the endpoint drops state after about ten minutes - so an entry
    that is not written down at entry time is gone.
    """
    head = rpc.block_number()
    earlier = head - window_blocks
    later = head - 2

    kinds = {k: [r for r in rows if r.get("kind") == k] for k in QUOTABLE}
    then: dict[str, dict] = {}
    now: dict[str, dict] = {}

    # The older read goes first: the head moves about ten blocks a second and
    # `earlier` walks out of the served window while the batch is in flight.
    if kinds["erc4626_usdg_vault"]:
        then["vault"] = _vault_state(kinds["erc4626_usdg_vault"], earlier)
    if kinds["uniswap_v3_pool"]:
        then["v3"] = _pool_state(kinds["uniswap_v3_pool"], earlier)
    if kinds["uniswap_v2_pool"]:
        then["v2"] = _v2_state(kinds["uniswap_v2_pool"], earlier)

    if kinds["erc4626_usdg_vault"]:
        now["vault"] = _vault_state(kinds["erc4626_usdg_vault"], later)
    if kinds["uniswap_v3_pool"]:
        now["v3"] = _pool_state(kinds["uniswap_v3_pool"], later)
    if kinds["uniswap_v2_pool"]:
        now["v2"] = _v2_state(kinds["uniswap_v2_pool"], later)

    blocks = rpc.batch([("eth_getBlockByNumber", [hex(earlier), False]),
                        ("eth_getBlockByNumber", [hex(later), False])])
    ts_then = rpc.to_int(blocks[0]["timestamp"])
    ts_now = rpc.to_int(blocks[1]["timestamp"])
    dt = ts_now - ts_then

    eth_usdg = reference_pool(later)["eth_usdg"]

    out: list[dict] = []
    if kinds["morpho_market"]:
        out += _quote_morpho(kinds["morpho_market"], later, ts_now)
    exit_sims: dict[str, dict] = {}
    if exit_usdg is not None and kinds["erc4626_usdg_vault"]:
        dec = _cfg()["quote_asset_decimals"]
        raw = abi.str_to_fixed(f"{exit_usdg:.{dec}f}", dec)
        for r in kinds["erc4626_usdg_vault"]:
            a = r["address"].lower()
            exit_sims[a] = exit_simulation(r["address"], raw, later,
                                           (exit_holders or {}).get(a, ()),
                                           now["vault"].get(a))

    scan_error = None
    if not vault_rates:
        vault_events = {}
        scan_error = "vault rate not read: a settlement quote skips the rate scan"
    else:
        try:
            vault_events = _vault_event_prices(kinds["erc4626_usdg_vault"], later)
        except rpc.RpcError as exc:
            # The event scan is the one batch the public endpoint throttles out
            # (cycles 40, 46, 51). Losing vault rates is a worse quote; losing the
            # whole board is no quote. Every vault row then carries rate_error.
            print(f"warning: vault rate scan failed, quoting vaults without a rate: {exc}",
                  file=sys.stderr)
            vault_events = {}
            scan_error = f"vault rate scan throttled: {str(exc)[:120]}"
    for row in kinds["erc4626_usdg_vault"]:
        vq = _quote_vault(row, then["vault"], now["vault"], later, ts_now, dt,
                          vault_events)
        if scan_error and vq.get("advertised_apy") is None:
            # "unknown because throttled" is worth a single-venue re-quote;
            # "unknown because the vault has no history" is not (cycle 62).
            vq["rate_error"] = scan_error
        sim = exit_sims.get(row["address"].lower())
        if sim is not None:
            vq["settle_state"]["exit_simulation"] = sim
        out.append(vq)
    for row in kinds["uniswap_v3_pool"]:
        out.append(_quote_v3(row, then["v3"], now["v3"], later, ts_now, dt, eth_usdg))
    for row in kinds["uniswap_v2_pool"]:
        out.append(_quote_v2(row, then["v2"], now["v2"], later, ts_now, dt, eth_usdg))
    if include_idle:
        out.append(idle_quote(later, ts_now))
    return out


PROBE_FROM = "0x000000000000000000000000000000000000dEaD"


def _revert_text(exc: Exception) -> str:
    """The endpoint's error object without the calldata echo in front of it."""
    return str(exc).split("]: ", 1)[-1][:400]


def recent_depositors(vault: str, head: int, lookback: int = VAULT_RATE_LOOKBACK_BLOCKS,
                      limit: int = 3) -> list[str]:
    """Owners of the largest recent deposits into `vault`, largest first. They
    are the addresses a withdraw can be simulated from: they hold shares."""
    topics = [[abi.TOPICS["Deposit(address,address,uint256,uint256)"]]]
    logs = rpc.scan_logs(max(1, head - lookback), head, [vault], topics, span=500_000)
    best: dict[str, int] = {}
    for lg in logs:
        tops = lg.get("topics") or []
        if len(tops) < 3 or len(lg["data"]) < 2 + 128:
            continue
        owner = "0x" + tops[2][-40:]
        best[owner] = max(best.get(owner, 0), abi.decode_uint(lg["data"], 0))
    return [o for o, _ in sorted(best.items(), key=lambda kv: -kv[1])[:limit]]


def simulate_withdraw(vault: str, assets_raw: int, holders: list[str],
                      block: int | str = "latest") -> dict:
    """Whether a withdraw of `assets_raw` executes right now, by `eth_call`
    from each holder in turn. A wrapper vault such as an Arcadia tranche holds
    no USDG itself, so its own balance says nothing; only a simulated withdraw
    does. A holder with too few shares reverts for the wrong reason, which is
    why the callers are the largest recent depositors and more than one."""
    errors: list[str] = []
    for h in holders:
        data = abi.encode_call("withdraw(uint256,address,address)", assets_raw, h, h)
        try:
            rpc.eth_call(vault, data, block=block, from_=h)
            return {"ok": True, "assets": str(assets_raw), "holder": h}
        except rpc.RpcError as exc:
            errors.append(f"{h[:10]}: {_revert_text(exc)}")
    return {"ok": False, "assets": str(assets_raw), "holder": None, "errors": errors,
            "note": ("no recent depositor could withdraw this amount" if holders
                     else "no recent depositor found to simulate with")}


def exit_simulation(vault: str, assets_raw: int, block: int | str,
                    known: list[str] | tuple = (), totals: dict | None = None) -> dict:
    """Whether a withdraw of `assets_raw` executes, simulated from `known`
    holders first and from a fresh depositor scan only when they cannot say.

    The scan reads seven days of Deposit logs, about 3,000 calls, and
    `a00036` paid it every hour for days while the Flock vault held no idle
    USDG. A known holder whose shares are worth at least the withdraw is as
    good a caller as any the scan could find: a success from them is a
    success, and a revert from them is the vault refusing, not the holder
    lacking shares. So the scan runs only when no known holder succeeds or
    holds enough. `totals` is the vault's `total_assets` and `total_shares`
    at `block`, which price a holder's shares; `convertToAssets` reverts on
    some vaults (Flock Credit Vault, cycle 216)."""
    known = list(dict.fromkeys(h.lower() for h in known if h))
    if known:
        sim = simulate_withdraw(vault, assets_raw, known, block)
        if sim["ok"]:
            return sim
        backed = _holders_backing(vault, assets_raw, known, block, totals)
        if backed:
            sim["note"] = (f"{backed[0]} holds shares worth at least the withdraw, so "
                           "the revert is the vault's, not the holder's")
            return sim
    return simulate_withdraw(vault, assets_raw, recent_depositors(vault, block), block)


def _holders_backing(vault: str, assets_raw: int, holders: list[str],
                     block: int | str, totals: dict | None) -> list[str]:
    """The holders whose shares are worth at least `assets_raw` at `block`,
    priced pro rata from the vault's totals. None of them when the totals
    are missing: unpriced shares prove nothing."""
    ta, ts = (totals or {}).get("total_assets"), (totals or {}).get("total_shares")
    if not ta or not ts:
        return []
    outs = rpc.eth_call_batch([(vault, abi.encode_call("balanceOf(address)", h))
                               for h in holders], block=block, allow_errors=True)
    return [h for h, out in zip(holders, outs)
            if isinstance(out, str) and len(out) >= 66
            and abi.decode_uint(out) * ta // ts >= assets_raw]


def probe(row: dict, size_usdg: float) -> dict:
    """Simulate the state-changing calls a position needs, from an address
    that holds nothing. A real vault fails on the token transfer (the caller
    has no USDG and no allowance); a vault that fails before that, with its
    own error, refuses every depositor. The raw outcome is returned so the
    reader judges, and nothing here is written anywhere."""
    dec = _cfg()["quote_asset_decimals"]
    raw = abi.str_to_fixed(f"{size_usdg:.{dec}f}", dec)
    out = {"venue": row["address"], "kind": row.get("kind"), "size_usdg": f"{size_usdg:.{dec}f}",
           "from": PROBE_FROM, "block": rpc.block_number(), "calls": {}}
    if row.get("kind") != "erc4626_usdg_vault":
        out["note"] = "probe simulates ERC-4626 vaults; Morpho free liquidity is in the quote"
        return out
    a = row["address"]
    sims = {
        "previewDeposit": (a, abi.encode_call("previewDeposit(uint256)", raw), None),
        "maxDeposit": (a, abi.encode_call("maxDeposit(address)", PROBE_FROM), None),
        "deposit": (a, abi.encode_call("deposit(uint256,address)", raw, PROBE_FROM), PROBE_FROM),
        "withdraw": (a, abi.encode_call("withdraw(uint256,address,address)",
                                        raw, PROBE_FROM, PROBE_FROM), PROBE_FROM),
        "usdg_balance_of_vault": (_cfg()["quote_asset_address"],
                                  abi.encode_call("balanceOf(address)", a), None),
        "totalAssets": (a, abi.encode_call("totalAssets()"), None),
    }
    for name, (to, data, frm) in sims.items():
        try:
            res = rpc.eth_call(to, data, from_=frm)
            value = abi.decode_uint(res) if len(res) >= 66 else None
            out["calls"][name] = {"ok": True, "raw": res[:74],
                                  "value": usdg_str(value) if value is not None and name != "previewDeposit" else value}
        except rpc.RpcError as exc:
            out["calls"][name] = {"ok": False, "error": _revert_text(exc)}
    dep = out["calls"]["deposit"]
    out["verdict"] = ("deposit simulated" if dep["ok"] else
                      "deposit reverts from an empty caller; read the error: a token "
                      "transfer failure is expected, anything else is the vault refusing")
    holders = recent_depositors(a, out["block"])
    out["recent_depositors"] = holders
    out["withdraw_simulation"] = simulate_withdraw(a, raw, holders, out["block"])
    return out


def idle_quote(block: int, ts: int) -> dict:
    """Idle is a venue. It pays nothing, costs nothing, and has no ceiling, and
    it is quoted like any other so the allocator can compare against it."""
    return {"venue": "idle", "kind": "idle", "block": block, "ts": ts,
            "depth_usdg": None, "advertised_apy": 0.0,
            "rate_source": "capital held in USDG earns nothing on 4663",
            "rate_window_seconds": None, "settle_state": {}}


# -- Morpho -----------------------------------------------------------------

def _quote_morpho(rows: list[dict], block: int, ts: int) -> list[dict]:
    """Supply APY from the market's own IRM, not from a published figure.

    `borrowRateView` returns a per-second rate in WAD. The supply side earns
    the borrow rate scaled by utilization and net of the market fee.
    """
    usdg = _cfg()["quote_asset_address"]
    calls = [(r["address"], abi.encode_call("market(bytes32)", r["market_id"])) for r in rows]
    outs = rpc.eth_call_batch(calls, block=block, allow_errors=True)

    quotes, rateable = [], []
    for row, out in zip(rows, outs):
        q = {"venue": row["address"], "market_id": row["market_id"],
             "kind": "morpho_market", "block": block, "ts": ts,
             "lltv": row.get("lltv"), "advertised_apy": None,
             "rate_window_seconds": None,
             "rate_source": f"{row['address']} market(bytes32) + irm borrowRateView at block {block}"}
        if not isinstance(out, str) or len(out) < 2 + 64 * 6:
            q["error"] = f"market(bytes32) did not answer: {str(out)[:100]}"
            q["depth_usdg"] = None
            quotes.append(q)
            continue
        w = abi.decode_uints(out, 6)
        st = {"total_supply_assets": str(w[0]), "total_supply_shares": str(w[1]),
              "total_borrow_assets": str(w[2]), "total_borrow_shares": str(w[3]),
              "last_update": str(w[4]), "market_fee": str(w[5])}
        q["settle_state"] = st
        q["depth_usdg"] = usdg_str(w[0])
        q["utilization"] = (w[2] / w[0]) if w[0] else 0.0
        quotes.append(q)
        if w[0] > 0 and int(row.get("irm", "0x0"), 16) != 0:
            rateable.append((q, row, w))

    if rateable:
        sel = abi.SELECTORS[BORROW_RATE_VIEW][2:]
        calls = []
        for _, row, w in rateable:
            body = (abi.encode_address(usdg)
                    + abi.encode_address(row["collateral_token"])
                    + abi.encode_address(row["oracle"])
                    + abi.encode_address(row["irm"])
                    + abi.encode_uint(abi.str_to_fixed(row["lltv"], 18))
                    + b"".join(abi.encode_uint(x) for x in w))
            calls.append((row["irm"], "0x" + sel + body.hex()))
        outs = rpc.eth_call_batch(calls, block=block, allow_errors=True)
        for (q, _row, w), out in zip(rateable, outs):
            if not isinstance(out, str) or len(out) < 66:
                q["rate_error"] = str(out)[:100]
                continue
            rate_wad = abi.decode_uint(out)
            per_second = rate_wad / WAD
            borrow_apy = math.expm1(per_second * SECONDS_YEAR)
            q["borrow_apy"] = borrow_apy
            # Morpho's stored market state only moves when someone touches the
            # market, so two stored share prices days apart can be identical
            # while interest was accruing the whole time. `core/settle.py`
            # replays the market's own accrual to close that gap, and this is
            # the rate it needs. It cannot be re-read later: the endpoint drops
            # state after ten minutes.
            q["settle_state"]["borrow_rate_wad"] = str(rate_wad)
            q["advertised_apy"] = borrow_apy * q["utilization"] * (1 - w[5] / WAD)
    return quotes


# -- ERC-4626 vaults --------------------------------------------------------

def _vault_state(rows: list[dict], block: int | str) -> dict[str, dict]:
    calls, index = [], []
    for r in rows:
        unit = 10 ** int(r.get("share_decimals", 18))
        calls += [(r["address"], abi.encode_call("convertToAssets(uint256)", unit)),
                  (r["address"], abi.encode_call("totalAssets()")),
                  (r["address"], abi.encode_call("totalSupply()")),
                  # The vault's own USDG balance: what a withdraw can actually
                  # pay out. A lending vault at high utilization reverts a
                  # withdraw exactly as a Morpho market does (a00006, cycle 16).
                  (_cfg()["quote_asset_address"],
                   abi.encode_call("balanceOf(address)", r["address"]))]
        index += [(r["address"].lower(), k) for k in
                  ("share_price", "total_assets", "total_shares", "idle_assets")]
    outs = rpc.eth_call_batch(calls, block=block, allow_errors=True)
    state: dict[str, dict] = {}
    for (addr, key), out in zip(index, outs):
        value = abi.decode_uint(out) if isinstance(out, str) and len(out) >= 66 else None
        state.setdefault(addr, {})[key] = value
    # Some vaults answer convertToAssets with nothing usable (Flock Credit
    # Vault, cycle 216). totalAssets/totalSupply is the same ratio, so price
    # the share from it rather than record a state no settlement can use.
    units = {r["address"].lower(): 10 ** int(r.get("share_decimals", 18)) for r in rows}
    for addr, st in state.items():
        if not st.get("share_price") and st.get("total_assets") and st.get("total_shares"):
            st["share_price"] = st["total_assets"] * units[addr] // st["total_shares"]
            st["share_price_source"] = "totalAssets/totalSupply"
    return state


def _vault_event_prices(rows: list[dict], head: int,
                        lookback: int = VAULT_RATE_LOOKBACK_BLOCKS) -> dict[str, dict]:
    """Share price at two points days apart, from each vault's own Deposit and
    Withdraw events.

    Both events carry the assets and the shares of the transfer, so their ratio
    is the share price at a block whose state can no longer be read. A deposit
    of a thousand USDG pins that ratio to nine significant digits, against the
    six `convertToAssets` offers, which is why the rate comes from here.

    Two things this cannot see. A vault that charges an entry or exit fee
    prices its events off the fee, so its rate reads low by roughly the fee;
    and a vault with no large transfer in the window gets no rate at all rather
    than a made-up one. Settlement is unaffected either way: a realized return
    comes from the share price the position itself moved through.
    """
    if not rows:
        return {}
    topics = [[abi.TOPICS["Deposit(address,address,uint256,uint256)"],
               abi.TOPICS["Withdraw(address,address,address,uint256,uint256)"]]]
    addresses = [r["address"] for r in rows]
    floor = VAULT_RATE_MIN_ASSETS_USDG * 10 ** _cfg()["quote_asset_decimals"]

    logs = rpc.scan_logs(max(1, head - lookback), head, addresses, topics, span=500_000)
    samples: dict[str, list[tuple[int, float, int]]] = {}
    for lg in logs:
        data = lg["data"]
        if len(data) < 2 + 128:
            continue
        assets, shares = abi.decode_uint(data, 0), abi.decode_uint(data, 1)
        if assets < floor or shares <= 0:
            continue
        samples.setdefault(lg["address"].lower(), []).append(
            (rpc.to_int(lg["blockNumber"]), assets / shares, assets))

    wanted: set[int] = set()
    ends: dict[str, tuple] = {}
    for addr, rows_ in samples.items():
        rows_.sort()
        first, last = rows_[0], rows_[-1]
        if first[0] == last[0]:
            continue
        ends[addr] = (first, last, len(rows_))
        wanted.update((first[0], last[0]))
    if not wanted:
        return {}

    order = sorted(wanted)
    blocks = rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in order])
    stamp = {b: rpc.to_int(blk["timestamp"]) for b, blk in zip(order, blocks)
             if isinstance(blk, dict)}

    out: dict[str, dict] = {}
    for addr, (first, last, n) in ends.items():
        t0, t1 = stamp.get(first[0]), stamp.get(last[0])
        if t0 is None or t1 is None or t1 - t0 < VAULT_RATE_MIN_SECONDS:
            continue
        out[addr] = {"from_block": first[0], "to_block": last[0],
                     "seconds": t1 - t0, "samples": n,
                     "price_from": first[1], "price_to": last[1],
                     "apy": (last[1] / first[1] - 1) * SECONDS_YEAR / (t1 - t0)}
    return out


def _quote_vault(row: dict, then: dict, now: dict, block: int, ts: int, dt: int,
                 events: dict[str, dict] | None = None) -> dict:
    """Depth and settlement state from live reads, the rate from events.

    The state window gives a share price accurate to six decimals over seven
    minutes, which resolves nothing: it reads zero for every vault on the chain
    most of the time. So `advertised_apy` comes from `_vault_event_prices` over
    days, and the state-window reading is kept beside it as
    `state_window_apy` - a floor of what state alone could have shown.
    """
    a = row["address"].lower()
    t, n = then.get(a, {}), now.get(a, {})
    unit = 10 ** int(row.get("share_decimals", 18))
    q = {"venue": row["address"], "kind": "erc4626_usdg_vault", "block": block, "ts": ts,
         "name": row.get("name"), "advertised_apy": None,
         "rate_window_seconds": None, "rate_source": None,
         "depth_usdg": usdg_str(n["total_assets"]) if n.get("total_assets") is not None else None,
         "settle_state": {"share_price": str(n.get("share_price")),
                          "share_unit": str(unit),
                          "total_assets": str(n.get("total_assets")),
                          "total_shares": str(n.get("total_shares")),
                          "idle_assets": str(n.get("idle_assets"))}}

    p0, p1 = t.get("share_price"), n.get("share_price")
    if p0 and p1 and dt > 0:
        q["state_window_apy"] = (p1 / p0 - 1) * SECONDS_YEAR / dt
        q["state_window_resolution_apy"] = (1 / p0) * SECONDS_YEAR / dt

    ev = (events or {}).get(a)
    if ev:
        q["advertised_apy"] = ev["apy"]
        q["rate_window_seconds"] = ev["seconds"]
        q["rate_samples"] = ev["samples"]
        q["rate_source"] = (f"assets/shares from {ev['samples']} Deposit and Withdraw "
                            f"events over {ev['seconds'] / 86400:.1f} days, blocks "
                            f"{ev['from_block']}..{ev['to_block']}")
    else:
        q["rate_error"] = (f"no Deposit or Withdraw above {VAULT_RATE_MIN_ASSETS_USDG} "
                           f"USDG far enough apart in the last "
                           f"{VAULT_RATE_LOOKBACK_BLOCKS:,} blocks")
    return q


# -- Uniswap ----------------------------------------------------------------

def _quote_v3(row: dict, then: dict, now: dict, block: int, ts: int,
              dt: int, eth_usdg: float) -> dict:
    """Fee yield from `feeGrowthGlobal` deltas, which is fees actually credited
    to in-range liquidity, not volume multiplied by the fee tier.

    This is a gross fee rate. It carries no impermanent-loss deduction, so it
    is the least comparable number on the board and is labelled as such.
    """
    a = row["address"].lower()
    t, n = then.get(a, {}), now.get(a, {})
    q = {"venue": row["address"], "kind": "uniswap_v3_pool", "block": block, "ts": ts,
         "fee_ppm": n.get("fee_ppm", row.get("fee_ppm")), "advertised_apy": None,
         "rate_window_seconds": dt, "depth_usdg": None,
         "rate_source": f"feeGrowthGlobal delta over {dt}s ending at block {block}, "
                        "gross of impermanent loss"}
    if n.get("error") or "usdg_raw" not in n:
        q["error"] = n.get("error", "pool state unreadable")
        return q

    other_units = n["other_raw"] / 10 ** n["other_decimals"]
    tvl = n["usdg_raw"] / 10 ** _cfg()["quote_asset_decimals"] + other_units * eth_usdg
    q["depth_usdg"] = abi.fixed_to_str(round(tvl * 10 ** 6), 6)
    q["settle_state"] = {
        "sqrt_price_x96": str(n["sqrt_price_x96"]), "tick": str(n["tick"]),
        "liquidity": str(n["liquidity"]), "fee_growth_0": str(n["fee_growth_0"]),
        "fee_growth_1": str(n["fee_growth_1"]), "usdg_raw": str(n["usdg_raw"]),
        "other_raw": str(n["other_raw"]), "other_token": n["other_token"],
        "other_decimals": str(n["other_decimals"]),
        "usdg_is_token0": str(n["usdg_is_token0"]), "eth_usdg": repr(eth_usdg)}

    if t.get("fee_growth_0") is None or dt <= 0 or not tvl:
        q["rate_error"] = "no earlier fee growth inside the served state window"
        return q
    d0 = n["fee_growth_0"] - t["fee_growth_0"]
    d1 = n["fee_growth_1"] - t["fee_growth_1"]
    liq = n["liquidity"]
    usdg_dec, other_dec = _cfg()["quote_asset_decimals"], n["other_decimals"]
    if n["usdg_is_token0"]:
        fees = (d0 / Q128 * liq / 10 ** usdg_dec) + (d1 / Q128 * liq / 10 ** other_dec) * eth_usdg
    else:
        fees = (d0 / Q128 * liq / 10 ** other_dec) * eth_usdg + (d1 / Q128 * liq / 10 ** usdg_dec)
    q["fees_usdg_window"] = abi.fixed_to_str(round(fees * 10 ** 6), 6)
    q["advertised_apy"] = (fees / tvl) * (SECONDS_YEAR / dt)
    return q


def _v2_state(rows: list[dict], block: int | str) -> dict[str, dict]:
    usdg = _cfg()["quote_asset_address"].lower()
    calls, index = [], []
    for r in rows:
        a = r["address"]
        for sig in ("getReserves()", "totalSupply()", "token0()", "token1()"):
            calls.append((a, abi.encode_call(sig)))
            index.append((a.lower(), sig))
    outs = rpc.eth_call_batch(calls, block=block, allow_errors=True)
    raw: dict[str, dict] = {}
    for (addr, sig), out in zip(index, outs):
        raw.setdefault(addr, {})[sig] = out

    follow, findex = [], []
    for addr, r in raw.items():
        try:
            t0 = abi.decode_address(r["token0()"]).lower()
            t1 = abi.decode_address(r["token1()"]).lower()
        except Exception:
            continue
        r["_other"] = t1 if t0 == usdg else t0
        r["_usdg_is_token0"] = t0 == usdg
        follow.append((r["_other"], abi.encode_call("decimals()")))
        findex.append(addr)
    outs = rpc.eth_call_batch(follow, block=block, allow_errors=True)
    for addr, out in zip(findex, outs):
        raw[addr]["_other_decimals"] = (
            abi.decode_uint(out) if isinstance(out, str) and len(out) >= 66 else 18)

    state: dict[str, dict] = {}
    for addr, r in raw.items():
        s = {"address": addr}
        try:
            s["reserve0"] = abi.decode_uint(r["getReserves()"], 0)
            s["reserve1"] = abi.decode_uint(r["getReserves()"], 1)
            s["total_supply"] = abi.decode_uint(r["totalSupply()"])
            s["usdg_is_token0"] = r["_usdg_is_token0"]
            s["other_token"] = r["_other"]
            s["other_decimals"] = r["_other_decimals"]
        except Exception as exc:                       # noqa: BLE001
            s["error"] = f"{type(exc).__name__}: {exc}"[:140]
        state[addr] = s
    return state


def _quote_v2(row: dict, then: dict, now: dict, block: int, ts: int,
              dt: int, eth_usdg: float) -> dict:
    """Fee yield from the growth in sqrt(k) per LP share, which is the only
    place a constant-product pool records the fees it kept."""
    a = row["address"].lower()
    t, n = then.get(a, {}), now.get(a, {})
    q = {"venue": row["address"], "kind": "uniswap_v2_pool", "block": block, "ts": ts,
         "fee_ppm": row.get("fee_ppm"), "advertised_apy": None,
         "rate_window_seconds": dt, "depth_usdg": None,
         "rate_source": f"sqrt(k)/share growth over {dt}s ending at block {block}, "
                        "gross of impermanent loss"}
    if n.get("error") or "reserve0" not in n:
        q["error"] = n.get("error", "pair state unreadable")
        return q
    usdg_dec = _cfg()["quote_asset_decimals"]
    usdg_raw = n["reserve0"] if n["usdg_is_token0"] else n["reserve1"]
    other_raw = n["reserve1"] if n["usdg_is_token0"] else n["reserve0"]
    tvl = usdg_raw / 10 ** usdg_dec + other_raw / 10 ** n["other_decimals"] * eth_usdg
    q["depth_usdg"] = abi.fixed_to_str(round(tvl * 10 ** 6), 6)
    q["settle_state"] = {"reserve0": str(n["reserve0"]), "reserve1": str(n["reserve1"]),
                         "total_supply": str(n["total_supply"]),
                         "usdg_is_token0": str(n["usdg_is_token0"]),
                         "other_token": n["other_token"],
                         "other_decimals": str(n["other_decimals"]),
                         "eth_usdg": repr(eth_usdg)}
    if "reserve0" not in t or dt <= 0 or not t.get("total_supply"):
        q["rate_error"] = "no earlier reserves inside the served state window"
        return q
    then_k = math.sqrt(t["reserve0"] * t["reserve1"]) / t["total_supply"]
    now_k = math.sqrt(n["reserve0"] * n["reserve1"]) / n["total_supply"]
    if then_k <= 0:
        q["rate_error"] = "empty pair at the start of the window"
        return q
    q["advertised_apy"] = (now_k / then_k - 1) * SECONDS_YEAR / dt
    return q


def board(kind: str | None = None, venue: str | None = None, top: int = 0,
          rank_by: str = "depth", min_depth: float = 0.0,
          include_idle: bool = True) -> list[dict]:
    """The quote a cycle reads: recorded rows picked by `rank`, quoted live,
    and the quotes that came back under `min_depth` dropped."""
    rows = [r for r in select(load(), kind=kind, venue=venue) if r.get("kind") in QUOTABLE]
    rows = rank(rows, by=rank_by, top=top, min_depth=min_depth)
    quotes = quote(rows, include_idle=include_idle)
    return [q for q in quotes
            if q["kind"] == "idle" or float(q.get("depth_usdg") or 0) >= min_depth]


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def discover(from_block: int | None = None) -> tuple[list[dict], int]:
    """Extend the record from chain events, then merge and save.

    Incremental by default: it scans forward from the newest block any recorded
    row was confirmed at. A full rescan is `--from-block 0`, which is expensive
    and rarely what a cycle wants.

    Scope, stated plainly: Morpho markets whose loan token is USDG, on every
    Morpho instance already recorded; USDG/WETH pools at every fee tier of
    every recorded Uniswap factory; and ERC-4626 vaults whose `asset()` is
    USDG. It does not find a venue whose family is not already in the record,
    and it does not price a pool whose other side is not the reference token.
    """
    existing = load()
    head = rpc.block_number()
    if from_block is None:
        seen = [int(r.get("confirmed_block", 0) or 0) for r in existing]
        from_block = max(seen) if seen else 0
    found: list[dict] = []
    found += _discover_morpho(existing, from_block, head)
    found += _discover_pools(existing, head)
    found += _discover_vaults(existing, from_block, head)
    rows, new = merge(existing, found)
    save(rows)
    return rows, new


def _stamp(block: int) -> dict:
    return {"confirmed_block": block,
            "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _discover_morpho(existing: list[dict], from_block: int, head: int) -> list[dict]:
    usdg = _cfg()["quote_asset_address"].lower()
    instances = sorted({r["address"] for r in existing
                        if r.get("kind") in ("morpho_blue", "morpho_blue_fork")})
    if not instances:
        return []
    topic = abi.TOPICS["CreateMarket(bytes32,(address,address,address,address,uint256))"]
    logs = rpc.scan_logs(from_block, head, instances, [topic], span=200_000)
    out = []
    for lg in logs:
        data = lg["data"]
        if len(data) < 2 + 64 * 5:
            continue
        loan = abi.decode_address(data, 0)
        if loan.lower() != usdg:
            continue
        out.append({"address": lg["address"].lower(),
                    "kind": "morpho_market",
                    "market_id": lg["topics"][1],
                    "loan_token": _cfg()["quote_asset"],
                    "collateral_token": abi.decode_address(data, 1),
                    "oracle": abi.decode_address(data, 2),
                    "irm": abi.decode_address(data, 3),
                    "lltv": abi.fixed_to_str(abi.decode_uint(data, 4), 18),
                    "created_block": rpc.to_int(lg["blockNumber"]),
                    "how": "CreateMarket event on the Morpho instance, loanToken "
                           "decoded from the event data",
                    **_stamp(head)})
    return out


def _discover_pools(existing: list[dict], head: int) -> list[dict]:
    """USDG paired with the reference token, resolved from each recorded
    factory rather than from an address list."""
    usdg = _cfg()["quote_asset_address"]
    other = _reference_token(existing)
    if other is None:
        return []
    v3 = [r["address"] for r in existing if r.get("kind", "").startswith("uniswap_v3_factory")]
    v2 = [r["address"] for r in existing if r.get("kind") == "uniswap_v2_factory"]
    calls, index = [], []
    for f in v3:
        for fee in V3_FEE_TIERS:
            calls.append((f, abi.encode_call("getPool(address,address,uint24)", usdg, other, fee)))
            index.append(("uniswap_v3_pool", f, fee))
    for f in v2:
        calls.append((f, abi.encode_call("getPair(address,address)", usdg, other)))
        index.append(("uniswap_v2_pool", f, 3000))
    outs = rpc.eth_call_batch(calls, allow_errors=True)
    out = []
    for (kind, factory, fee), res in zip(index, outs):
        if not isinstance(res, str) or len(res) < 66:
            continue
        addr = abi.decode_address(res)
        if int(addr, 16) == 0:
            continue
        out.append({"address": addr.lower(), "kind": kind, "factory": factory,
                    "fee_ppm": fee, "pair": f"{_cfg()['quote_asset']}/other",
                    "other_token": other,
                    "how": "resolved from the recorded factory by "
                           "getPool/getPair(USDG, reference token)",
                    **_stamp(head)})
    return out


def _reference_token(existing: list[dict]) -> str | None:
    """The token USDG is paired against in the recorded pools. Read from a pool
    rather than hardcoded, so the record stays the source of truth."""
    for row in existing:
        if row.get("other_token"):
            return row["other_token"]
    usdg = _cfg()["quote_asset_address"].lower()
    pools = [r for r in existing if r.get("kind") in ("uniswap_v3_pool", "uniswap_v2_pool")]
    if not pools:
        return None
    calls = []
    for p in pools[:8]:
        calls += [(p["address"], abi.encode_call("token0()")),
                  (p["address"], abi.encode_call("token1()"))]
    outs = rpc.eth_call_batch(calls, allow_errors=True)
    for i in range(0, len(outs), 2):
        try:
            t0, t1 = abi.decode_address(outs[i]), abi.decode_address(outs[i + 1])
        except Exception:
            continue
        return t1 if t0.lower() == usdg else t0
    return None


def _discover_vaults(existing: list[dict], from_block: int, head: int) -> list[dict]:
    """ERC-4626 vaults denominated in USDG.

    Found from the ERC-4626 `Deposit` event, not from a factory: no
    `CreateMetaMorpho` event exists anywhere on 4663, and the vaults that do
    exist are the deepest USDG venue on the chain.
    """
    usdg = _cfg()["quote_asset_address"].lower()
    topic = abi.TOPICS["Deposit(address,address,uint256,uint256)"]
    known = {r["address"].lower() for r in existing}
    logs = rpc.scan_logs(from_block, head, None, [topic], span=200_000)
    candidates = sorted({lg["address"].lower() for lg in logs} - known)
    if not candidates:
        return []
    outs = rpc.eth_call_batch(
        [(a, abi.encode_call("asset()")) for a in candidates], allow_errors=True)
    matched = []
    for addr, res in zip(candidates, outs):
        if isinstance(res, str) and len(res) >= 66:
            try:
                if abi.decode_address(res).lower() == usdg:
                    matched.append(addr)
            except Exception:
                continue
    if not matched:
        return []
    calls = []
    for a in matched:
        calls += [(a, abi.encode_call("totalAssets()")),
                  (a, abi.encode_call("decimals()")),
                  (a, abi.encode_call("name()"))]
    outs = rpc.eth_call_batch(calls, allow_errors=True)
    out = []
    for i, addr in enumerate(matched):
        ta, dec, name = outs[3 * i], outs[3 * i + 1], outs[3 * i + 2]
        out.append({"address": addr, "kind": "erc4626_usdg_vault",
                    "name": abi.decode_string(name) if isinstance(name, str) and len(name) > 2 else None,
                    "share_decimals": abi.decode_uint(dec) if isinstance(dec, str) and len(dec) >= 66 else 18,
                    "total_assets_usdg": usdg_str(abi.decode_uint(ta)) if isinstance(ta, str) and len(ta) >= 66 else None,
                    "how": "ERC-4626 Deposit emitter whose asset() is USDG; found by "
                           "scanning the Deposit topic, not by a factory event",
                    **_stamp(head)})
    return out


def refresh(min_depth_usdg: float = 0.0) -> list[dict]:
    """Re-read the live fields on every quotable row and rewrite the record.

    Only depth, the advertised rate, and the block they were read at change. A
    row's confirming evidence is never rewritten.
    """
    rows = load()
    quotable = [r for r in rows if r.get("kind") in QUOTABLE and r.get("kind") != "idle"]
    quotes = quote(quotable, include_idle=False)
    by_key = {}
    for q in quotes:
        k = (f"{q['venue'].lower()}:{q['market_id'].lower()}"
             if q["kind"] == "morpho_market" else q["venue"].lower())
        by_key[k] = q
    for row in rows:
        q = by_key.get(_key(row))
        if q is None:
            continue
        depth = float(q["depth_usdg"]) if q.get("depth_usdg") is not None else 0.0
        if depth < min_depth_usdg:
            continue
        row["depth_usdg"] = q.get("depth_usdg")
        row["advertised_apy"] = q.get("advertised_apy")
        row["rate_window_seconds"] = q.get("rate_window_seconds")
        row["quoted_block"] = q["block"]
        row["quoted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(q["ts"]))
    save(rows)
    return rows


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _fmt_apy(value: object) -> str:
    return f"{'-':>10}" if value is None else f"{float(value):10.2%}"


def _fmt_depth(value: object) -> str:
    return f"{'-':>13}" if value is None else f"{float(value):13,.0f}"


def label(q: dict) -> str:
    """How a venue is named in a report: a Morpho market is a market id on an
    instance, everything else is an address."""
    if q.get("market_id"):
        return f"{q['market_id'][:26]}..@{q['venue'][:10]}"
    return q["venue"]


def _print_quotes(quotes: list[dict]) -> None:
    quotes = sorted(quotes, key=lambda q: -(float(q["depth_usdg"] or 0)))
    print(f"{'venue':42s} {'kind':19s} {'depth USDG':>13} {'advertised':>10}  note")
    for q in quotes:
        note = q.get("error") or q.get("rate_error") or q.get("name") or ""
        print(f"{label(q)[:42]:42s} {q['kind'][:19]:19s} "
              f"{_fmt_depth(q.get('depth_usdg'))} {_fmt_apy(q.get('advertised_apy'))}  {note[:44]}")


def _record_baselines(quotes: list[dict]) -> None:
    """Baseline forecast rows for a board just quoted, reported on stderr so
    `--json` output is unchanged. A failure here is a warning: losing the
    baselines is a worse cycle, losing the quote is no cycle."""
    from core import forecast          # forecast imports this module
    try:
        out = forecast.record_baselines(quotes)
    except (forecast.ForecastError, rpc.RpcError, VenueError, OSError) as exc:
        print(f"warning: baseline forecasts not recorded: {exc}", file=sys.stderr)
        return
    print(f"forecast: {len(out['recorded'])} baseline rows recorded, "
          f"{out['already_live']} already live, {len(out['skipped'])} venues skipped",
          file=sys.stderr)
    for s in out["skipped"]:
        print(f"forecast: skipped {s['venue'][:50]}: {s['reason'][:120]}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="print the recorded venues")
    p.add_argument("--kind")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("quote", help="live state and advertised rate")
    p.add_argument("--kind")
    p.add_argument("--venue", help="address or market id, a prefix is enough")
    p.add_argument("--top", type=int, default=0,
                   help="quote N recorded rows, picked by --rank-by")
    p.add_argument("--rank-by", choices=("depth", "rate"), default="depth",
                   help="how --top picks: deepest, or highest advertised rate at the last refresh")
    p.add_argument("--min-depth", type=float, default=0.0,
                   help="skip rows under this recorded depth before the pick, and quotes under it after")
    p.add_argument("--no-idle", action="store_true")
    p.add_argument("--no-record", action="store_true",
                   help="do not record baseline forecast rows for the quoted venues")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("probe", help="simulate deposit and withdraw on a vault")
    p.add_argument("--venue", required=True, help="address or market id, a prefix is enough")
    p.add_argument("--size", type=float, default=250.0)

    p = sub.add_parser("refresh", help="re-read depth and rate into the record")
    p.add_argument("--min-depth", type=float, default=0.0)

    p = sub.add_parser("discover", help="extend the record from chain events")
    p.add_argument("--from-block", type=int, default=None)

    args = ap.parse_args(argv)

    if args.cmd == "list":
        rows = select(load(), kind=args.kind)
        if args.json:
            print(json.dumps(rows, indent=1, sort_keys=True))
        else:
            for row in sorted(rows, key=lambda r: (r.get("kind", ""), _key(r))):
                print(f"{row.get('kind',''):26s} {row['address']} "
                      f"{row.get('market_id','')} {str(row.get('name') or '')[:24]}")
            print(f"\n{len(rows)} rows")
        return 0

    if args.cmd == "probe":
        rows = select(load(), venue=args.venue)
        if len(rows) != 1:
            print(f"{len(rows)} venues match {args.venue!r}; give a longer prefix", file=sys.stderr)
            return 2
        print(json.dumps(probe(rows[0], args.size), indent=1, sort_keys=True))
        return 0

    if args.cmd == "quote":
        quotes = board(kind=args.kind, venue=args.venue, top=args.top,
                       rank_by=args.rank_by, min_depth=args.min_depth,
                       include_idle=not args.no_idle)
        if not args.no_record:
            _record_baselines(quotes)
        if args.json:
            print(json.dumps(quotes, indent=1, sort_keys=True))
        else:
            _print_quotes(quotes)
            print(f"\nrpc {json.dumps(rpc.stats())}")
        return 0

    if args.cmd == "refresh":
        rows = refresh(args.min_depth)
        print(f"refreshed {len(rows)} rows in {VENUES_PATH.relative_to(ROOT)}")
        print(f"rpc {json.dumps(rpc.stats())}")
        return 0

    if args.cmd == "discover":
        rows, new = discover(args.from_block)
        print(f"{len(rows)} rows in {VENUES_PATH.relative_to(ROOT)}, {new} new")
        print(f"rpc {json.dumps(rpc.stats())}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
