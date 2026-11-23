"""The ledger: the only writer of `journal/allocations.jsonl`.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

Owns:
  - the row schema. One row is one allocation, from the moment it is opened to
    the moment it settles. Every USDG amount in it is a decimal string in whole
    USDG, never a raw integer and never a float;
  - the three rates, kept apart for the whole life of the row:
    `advertised_apy` is what the venue said, `forecast_net_apy` is what the
    agent committed to before allocating, and `realized_net_apy` is what the
    position actually netted. Nothing in this file can copy one into another;
  - the caps in `config/protected.json`, checked at write time. A write that
    breaks one raises and nothing reaches the file;
  - the entry state. The endpoint serves state for about ten minutes, so the
    on-chain numbers settlement will need are recorded at entry or they are
    gone. `entry_state` is that record;
  - the arithmetic of a settled outcome. `settle` is given the money the venue
    produced and the cost of getting out, and derives `realized_usdg` and
    `realized_net_apy` itself, so a caller cannot hand in a rate that does not
    follow from the amounts.

Idle is a venue. It pays nothing, costs nothing, and is exempt from the
allocation cap and the net-forecast floor, because refusing to record idle
capital would make doing nothing invisible in the score. It still takes up
bankroll, which is what stops the agent from parking the whole bankroll for a
week and allocating anyway.

There is no strategy judgment here. This file does not choose a venue, a size,
or a horizon, and does not decide whether a forecast is any good. It records
what it is given, refuses what the caps forbid, and does the arithmetic.

Commands:
    python3 core/ledger.py list [--status open] [--json]
    python3 core/ledger.py show a00001
    python3 core/ledger.py open --venue 0x1234 --size 250 --horizon 1440 \
        --gross-apy 0.0504 [--market-id 0xabc] [--dry-run]
    python3 core/ledger.py open --venue idle --size 750 --horizon 1440
    python3 core/ledger.py void a00003 --reason "quote was stale on write"
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import abi, cost, rpc, venues

LEDGER_PATH = ROOT / "journal" / "allocations.jsonl"

SECONDS_YEAR = 365 * 24 * 3600
STATUSES = ("open", "settled", "marked", "void")
# A marked row is closed against what the venue could pay when it would not
# pay the position back, and is scored like a settled row (see `settle`).
SCORED_STATUSES = ("settled", "marked")

# Fields that hold money. Every one of them is a decimal string in whole USDG.
AMOUNT_FIELDS = ("size_usdg", "entry_cost_usdg", "exit_cost_forecast_usdg",
                 "forecast_net_usdg", "exit_cost_usdg", "realized_gross_usdg",
                 "realized_usdg")

# Fields that may only exist on a settled row. An open row carrying any of
# them is a forecast written after the outcome, which is the one failure this
# repo cannot recover from.
REALIZED_FIELDS = ("exit_block", "exit_ts", "exit_cost_usdg",
                   "realized_gross_usdg", "realized_usdg", "realized_net_apy",
                   "settled_ts", "hold_minutes", "exit_state")

# What a loss mark leaves on a row, moved into `loss_mark` when it settles.
MARK_FIELDS = REALIZED_FIELDS + ("settle_note", "mark_reason")

REQUIRED_FIELDS = ("id", "ts", "cycle", "venue", "kind", "size_usdg",
                   "entry_block", "entry_ts", "entry_cost_usdg",
                   "advertised_apy", "forecast_gross_apy", "forecast_net_apy",
                   "forecast_net_bps", "forecast_horizon_min", "status")

AMOUNT_RE = re.compile(r"^-?\d+\.\d{6}$")


class LedgerError(Exception):
    """A write the caps or the schema refuse. Nothing reached the file."""


def _cfg() -> dict:
    return rpc.protected_config()


def _dec(value: object) -> Decimal:
    """A USDG amount as an exact decimal. Accepts the canonical string, and a
    float only from a live computation that has not been through the file."""
    return Decimal(str(value))


def usdg_amount(value: object) -> str:
    """The canonical form of a USDG amount: a decimal string in whole USDG at
    the token's own precision. Never a float, never a raw integer."""
    dec = _cfg()["quote_asset_decimals"]
    raw = _dec(value).scaleb(dec)
    return abi.fixed_to_str(int(raw.to_integral_value(rounding="ROUND_HALF_EVEN")), dec)


def net_apy(size_usdg: object, net_usdg: object, hold_minutes: float) -> float:
    """The annualized net rate of a position that made `net_usdg` on
    `size_usdg` over `hold_minutes`.

    Compounding-consistent, to match the rates the venues quote: a hold of `t`
    years returns `(1 + apy) ** t - 1`. A position that loses its whole
    principal floors at -1.0, because a JSON row cannot hold minus infinity and
    losing everything is already the worst outcome an annual rate can say.
    """
    size, net = float(_dec(size_usdg)), float(_dec(net_usdg))
    if size <= 0 or hold_minutes <= 0:
        return 0.0
    ratio = 1 + net / size
    if ratio <= 0:
        return -1.0
    years = hold_minutes * 60 / SECONDS_YEAR
    value = ratio ** (1 / years) - 1
    return max(value, -1.0) if math.isfinite(value) else -1.0


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------

def load(path: Path = LEDGER_PATH) -> list[dict]:
    """Every row, oldest first."""
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as err:
            raise LedgerError(f"{path.name} line {n} is not JSON: {err}") from err
    return rows


def save(rows: list[dict], path: Path = LEDGER_PATH) -> None:
    """Rewrite the file. Settlement edits a row in place, so this is a rewrite
    and not an append; it is atomic so a crash cannot leave half a ledger."""
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def next_id(rows: list[dict]) -> str:
    """Ledger ids are sequential so the file reads in the order it happened."""
    highest = 0
    for row in rows:
        match = re.fullmatch(r"a(\d+)", str(row.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"a{highest + 1:05d}"


def find(rows: list[dict], row_id: str) -> dict:
    for row in rows:
        if row["id"] == row_id:
            return row
    raise LedgerError(f"no allocation with id {row_id!r}")


def open_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("status") == "open"]


def default_cycle(ts: float | None = None) -> str:
    """The cycle an allocation belongs to, for the per-cycle cap. The loop runs
    hourly, so the hour is the cycle unless the caller names one."""
    return time.strftime("%Y-%m-%dT%HZ", time.gmtime(ts if ts is not None else time.time()))


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------

LIQUIDITY_CHECKED_KINDS = ("morpho_market", "erc4626_usdg_vault")


def free_liquidity(kind: str, state: dict) -> Decimal | None:
    """What the venue could pay a withdrawer right now, in whole USDG, from
    the state as recorded. For a Morpho market that is total supply minus
    total borrow; for a vault it is the vault's own USDG balance. None when
    the state does not carry the fields, which a caller must treat as
    unverifiable, not as zero and not as plenty. An inconclusive exit
    simulation (`ok` None: no holder with the shares was found to ask) is
    unverifiable too; `unverifiable_why` says which."""
    try:
        if kind == "morpho_market":
            raw = int(state["total_supply_assets"]) - int(state["total_borrow_assets"])
        elif kind == "erc4626_usdg_vault":
            sim = state.get("exit_simulation")
            if isinstance(sim, dict) and "ok" in sim:
                # A simulated withdraw is the truth for a wrapper vault, whose
                # own balance is zero by design (Arcadia tranches, a00008).
                if sim["ok"] is None:
                    return None
                raw = int(sim["assets"]) if sim["ok"] else 0
            else:
                raw = int(state["idle_assets"])
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return Decimal(raw).scaleb(-_cfg()["quote_asset_decimals"])


def unverifiable_why(state: dict) -> str:
    """Why `free_liquidity` returned None for `state`, for messages."""
    sim = state.get("exit_simulation")
    if isinstance(sim, dict) and "ok" in sim and sim["ok"] is None:
        return f"the exit simulation was {sim.get('note') or 'inconclusive'}"
    return "the state lacks the fields free liquidity is read from"


def free_liquidity_amount(sim: object) -> str:
    """The USDG amount a recorded exit simulation tried, for messages."""
    try:
        return usdg_amount(Decimal(int(sim["assets"])).scaleb(-_cfg()["quote_asset_decimals"]))  # type: ignore[index]
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return "?"


def morpho_free_liquidity(state: dict) -> Decimal | None:
    return free_liquidity("morpho_market", state)


def cap_violations(existing: list[dict], row: dict, cfg: dict | None = None) -> list[str]:
    """Every cap `row` would break if it were appended to `existing`.

    Returns them all rather than the first, so an operator reading a refusal
    sees the whole picture. Idle is exempt from the per-allocation cap, the
    open-position count, the venue share and the net floor - it is the absence
    of an allocation - but it consumes bankroll like anything else.
    """
    cfg = cfg or _cfg()
    out: list[str] = []
    idle = row.get("kind") == "idle"
    bankroll = _dec(cfg["sim_bankroll_usdg"])
    live = open_rows(existing)
    # `core/validate.py` replays hand-edited rows through here, so a missing or
    # unparseable field has to come back as a violation rather than a traceback.
    for field in REQUIRED_FIELDS:
        if field not in row:
            out.append(f"row is missing {field}")
    try:
        size = _dec(row.get("size_usdg", 0))
    except (ArithmeticError, ValueError):
        out.append(f"size_usdg {row.get('size_usdg')!r} is not a number")
        return out

    if cfg.get("real_execution_enabled"):
        out.append("real_execution_enabled is true: this ledger is the paper "
                   "ledger and core/real.py owns the real leg")
    if size <= 0:
        out.append(f"size_usdg {row.get('size_usdg')} is not positive")
    if not idle and size > _dec(cfg["max_allocation_usdg"]):
        out.append(f"size_usdg {row.get('size_usdg')} exceeds max_allocation_usdg "
                   f"{cfg['max_allocation_usdg']}")

    committed = sum((_dec(r.get("size_usdg", 0)) for r in live), Decimal(0))
    if committed + size > bankroll:
        out.append(f"size_usdg {row.get('size_usdg')} on top of {committed} already open "
                   f"exceeds sim_bankroll_usdg {cfg['sim_bankroll_usdg']}")

    if not idle:
        deployed = [r for r in live if r.get("kind") != "idle"]
        if len(deployed) >= int(cfg["max_open_allocations"]):
            out.append(f"{len(deployed)} allocations already open, "
                       f"max_open_allocations is {cfg['max_open_allocations']}")

        share_cap = bankroll * _dec(cfg["max_venue_share"])
        at_venue = sum((_dec(r.get("size_usdg", 0)) for r in deployed
                        if str(r.get("venue", "")).lower()
                        == str(row.get("venue", "")).lower()), Decimal(0))
        if at_venue + size > share_cap:
            out.append(f"{row.get('venue')} would hold {at_venue + size} USDG, over the "
                       f"max_venue_share ceiling of {share_cap} USDG")

        this_cycle = [r for r in existing
                      if r.get("cycle") == row.get("cycle") and r.get("kind") != "idle"]
        if len(this_cycle) >= int(cfg["max_new_allocations_per_cycle"]):
            out.append(f"cycle {row.get('cycle')} already opened {len(this_cycle)} "
                       f"allocations, max_new_allocations_per_cycle is "
                       f"{cfg['max_new_allocations_per_cycle']}")

        if float(row.get("forecast_net_bps", 0.0)) < float(cfg["min_expected_net_bps"]):
            out.append(f"forecast_net_bps {row.get('forecast_net_bps', 0.0):.2f} is below "
                       f"min_expected_net_bps {cfg['min_expected_net_bps']}. Leaving "
                       f"the capital idle is the legal move")

        # The best-paying Morpho market is the one with the least free
        # liquidity, because supply APY is borrow rate times utilization. A
        # market that cannot pay the position back at entry is not a venue.
        if row.get("kind") == "erc4626_usdg_vault":
            sp = (row.get("entry_state") or {}).get("share_price")
            if sp in (None, "", "None", "0", 0):
                out.append("entry_state has no share_price, so core/settle.py could "
                           "never settle this row; a row that cannot settle is worse "
                           "than no row")

        if row.get("kind") in LIQUIDITY_CHECKED_KINDS:
            free = free_liquidity(str(row.get("kind")), row.get("entry_state") or {})
            multiple = _dec(cfg["min_free_liquidity_multiple"])
            if free is None:
                out.append(f"entry_state: {unverifiable_why(row.get('entry_state') or {})}, "
                           "so free liquidity cannot be verified and a withdraw cannot "
                           "be shown to succeed")
            elif free < size * multiple:
                sim = (row.get("entry_state") or {}).get("exit_simulation")
                why = (f"a simulated withdraw of {free_liquidity_amount(sim)} USDG reverted "
                       f"for every recent depositor: {'; '.join(sim.get('errors') or [sim.get('note', '')])}"
                       if isinstance(sim, dict) and not sim.get("ok") else
                       f"free liquidity {free} USDG is under min_free_liquidity_multiple "
                       f"{multiple} x size {size} = {size * multiple} USDG")
                out.append(f"{why}: a withdraw would revert, so this position "
                           "could not be exited")

    if float(row.get("forecast_horizon_min", 0)) < float(cfg["min_hold_minutes"]):
        out.append(f"forecast_horizon_min {row.get('forecast_horizon_min')} is under "
                   f"min_hold_minutes {cfg['min_hold_minutes']}")

    if any(row.get(f) is not None for f in REALIZED_FIELDS):
        out.append("an open row carries a realized field: the forecast must be "
                   "written before the outcome, never after")
    for field in AMOUNT_FIELDS:
        if field in row and row[field] is not None and not AMOUNT_RE.fullmatch(str(row[field])):
            out.append(f"{field} {row[field]!r} is not a decimal string in whole USDG")
    if any(r.get("id") == row.get("id") for r in existing):
        out.append(f"id {row.get('id')} is already in the ledger")
    return out


# --------------------------------------------------------------------------
# Opening
# --------------------------------------------------------------------------

def build_row(quote: dict, size_usdg: object, horizon_min: int,
              forecast_gross_apy: float, trip: dict, cycle: str | None = None,
              note: str | None = None) -> dict:
    """An open row, from a live quote and a live round-trip cost.

    `forecast_gross_apy` is the agent's own number: the rate it believes the
    position will earn before costs. Passing the venue's advertised rate
    straight through is a legal forecast and a lazy one, and the score will say
    so. The net forecast is derived from it here so that the row can never
    claim a net the costs do not support.
    """
    kind = quote["kind"]
    size = usdg_amount(size_usdg)
    total_cost = _dec(trip["total_cost_usdg"])
    outlook = cost.net_of_cost(float(forecast_gross_apy), float(_dec(size)),
                               float(horizon_min), float(total_cost))
    row = {
        "id": None,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cycle": cycle or default_cycle(),
        "venue": quote["venue"],
        "kind": kind,
        "size_usdg": size,
        "entry_block": int(quote["block"]),
        "entry_ts": int(quote["ts"]),
        "entry_cost_usdg": usdg_amount(trip["entry_cost_usdg"]),
        "exit_cost_forecast_usdg": usdg_amount(trip["exit_cost_usdg"]),
        "advertised_apy": quote.get("advertised_apy"),
        "forecast_gross_apy": float(forecast_gross_apy),
        "forecast_net_apy": net_apy(size, outlook["net_usdg"], float(horizon_min)),
        "forecast_net_usdg": usdg_amount(outlook["net_usdg"]),
        "forecast_net_bps": outlook["net_bps"],
        "forecast_horizon_min": int(horizon_min),
        "status": "open",
        "entry_state": quote.get("settle_state") or {},
        "entry_depth_usdg": quote.get("depth_usdg"),
        "rate_source": quote.get("rate_source"),
        "gas_price_gwei": trip.get("gas_price_gwei"),
    }
    if quote.get("market_id"):
        row["market_id"] = quote["market_id"]
    if note:
        row["note"] = note
    return row


def append(row: dict, path: Path = LEDGER_PATH, cfg: dict | None = None) -> dict:
    """Check every cap, then write. Raises `LedgerError` and writes nothing if
    any cap refuses."""
    rows = load(path)
    row = dict(row)
    row["id"] = row.get("id") or next_id(rows)
    problems = cap_violations(rows, row, cfg)
    if problems:
        raise LedgerError(f"refused {row['id']} at {row['venue']}:\n  - "
                          + "\n  - ".join(problems))
    rows.append(row)
    save(rows, path)
    return row


def open_allocation(venue: str, size_usdg: object, horizon_min: int,
                    forecast_gross_apy: float | None = None,
                    market_id: str | None = None, cycle: str | None = None,
                    note: str | None = None, path: Path = LEDGER_PATH,
                    dry_run: bool = False) -> dict:
    """Quote the venue live, price the round trip live, and write the row.

    The live reads are the point: an entry priced from a cached quote is an
    entry priced at a state that no longer exists.
    """
    quote = live_quote(venue, market_id, size_usdg=size_usdg)
    gross = 0.0 if quote["kind"] == "idle" else forecast_gross_apy
    if gross is None:
        raise LedgerError("a non-idle allocation needs an explicit --gross-apy: "
                          "the forecast is the agent's, not the venue's")
    trip = cost.round_trip(quote["kind"], float(_dec(usdg_amount(size_usdg))))
    row = build_row(quote, size_usdg, horizon_min, gross, trip, cycle, note)
    if dry_run:
        rows = load(path)
        row["id"] = next_id(rows)
        row["dry_run"] = True
        row["would_refuse"] = cap_violations(rows, row) or None
        return row
    return append(row, path)


def live_quote(venue: str, market_id: str | None = None,
               size_usdg: object | None = None, holders: list[str] | tuple = (),
               vault_rates: bool = True) -> dict:
    """The one venue's live quote, including idle. With `size_usdg`, a vault
    quote also simulates withdrawing `min_free_liquidity_multiple` times that
    size, which is what the free-liquidity check then reads, trying
    `holders` before it scans for depositors. `vault_rates` is passed to
    `venues.quote`."""
    if venue.lower() == "idle":
        head = rpc.block_number() - 2
        block = rpc.get_block(head)
        return venues.idle_quote(head, rpc.to_int(block["timestamp"]))
    rows = [r for r in venues.load() if r.get("kind") in venues.QUOTABLE]
    if market_id:
        key = market_id.lower()
        rows = [r for r in rows
                if str(r.get("market_id", "")).lower().startswith(key)
                and r["address"].lower().startswith(venue.lower())]
    else:
        rows = venues.select(rows, venue=venue)
    if not rows:
        raise LedgerError(f"no recorded venue matches {venue!r}"
                          + (f" / {market_id!r}" if market_id else ""))
    if len(rows) > 1:
        names = ", ".join(r.get("market_id") or r["address"] for r in rows[:6])
        raise LedgerError(f"{len(rows)} venues match {venue!r}: {names}")
    exit_usdg = None
    if size_usdg is not None:
        exit_usdg = float(_dec(size_usdg) * _dec(_cfg()["min_free_liquidity_multiple"]))
    quotes = venues.quote(rows, include_idle=False, exit_usdg=exit_usdg,
                          exit_holders={r["address"].lower(): list(holders) for r in rows},
                          vault_rates=vault_rates)
    if not quotes:
        raise LedgerError(f"{venue!r} did not quote")
    quote = quotes[0]
    if quote.get("error"):
        raise LedgerError(f"{venue!r} did not quote: {quote['error']}")
    return quote


# --------------------------------------------------------------------------
# Settling and voiding
# --------------------------------------------------------------------------

def settle(row_id: str, exit_block: int, exit_ts: int, exit_cost_usdg: object,
           realized_gross_usdg: object, exit_state: dict | None = None,
           note: str | None = None, path: Path = LEDGER_PATH,
           cfg: dict | None = None, mark_reason: str | None = None) -> dict:
    """Close a row against what it actually produced.

    The caller supplies the money the venue produced before costs and what the
    exit cost. Everything else is derived here: `realized_usdg` is the gross
    less both legs of the round trip, and `realized_net_apy` follows from it and
    the hold. A caller cannot pass a rate in, so no advertised or forecast
    number can end up in a realized field.

    With `mark_reason`, an open row is loss-marked instead: status `marked`,
    booked at what the venue could pay at that block, and scored like a
    settled row. It stops counting against the caps. A marked row can still
    settle later, when the venue pays it back: the mark moves to `loss_mark`
    and `settled_ts` keeps the time the row first closed, because the book has
    been replayed without it since then (`core/validate.py`).
    """
    cfg = cfg or _cfg()
    rows = load(path)
    row = find(rows, row_id)
    allowed = ("open",) if mark_reason else ("open", "marked")
    if row["status"] not in allowed:
        raise LedgerError(f"{row_id} is {row['status']}, not {' or '.join(allowed)}")
    prior = {k: row.pop(k) for k in MARK_FIELDS if k in row} if row["status"] == "marked" else {}

    hold_minutes = (int(exit_ts) - int(row["entry_ts"])) / 60
    if hold_minutes < float(cfg["min_hold_minutes"]):
        raise LedgerError(f"{row_id} has been held {hold_minutes:.1f} minutes, under "
                          f"min_hold_minutes {cfg['min_hold_minutes']}. A hold shorter "
                          "than the floor is not scored")

    gross = _dec(realized_gross_usdg)
    exit_cost = _dec(exit_cost_usdg)
    if exit_cost < 0:
        raise LedgerError("exit_cost_usdg cannot be negative")
    net = gross - exit_cost - _dec(row["entry_cost_usdg"])

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    row.update({
        "status": "marked" if mark_reason else "settled",
        "exit_block": int(exit_block),
        "exit_ts": int(exit_ts),
        "exit_cost_usdg": usdg_amount(exit_cost),
        "realized_gross_usdg": usdg_amount(gross),
        "realized_usdg": usdg_amount(net),
        "realized_net_apy": net_apy(row["size_usdg"], usdg_amount(net), hold_minutes),
        "hold_minutes": round(hold_minutes, 2),
        "settled_ts": prior.get("settled_ts", now),
        "exit_state": exit_state or {},
    })
    if note:
        row["settle_note"] = note
    if mark_reason:
        row["mark_reason"] = mark_reason
    if prior:
        row["loss_mark"] = prior
        row["recovered_ts"] = now
    save(rows, path)
    return row


def void(row_id: str, reason: str, path: Path = LEDGER_PATH) -> dict:
    """Retire a row that cannot be settled honestly - a venue that stopped
    answering, an entry state that was never written. A void row scores
    nothing; it is not a zero, it is an absence, and the score counts them so
    a strategy cannot improve by voiding its losers."""
    if not reason:
        raise LedgerError("a void needs a reason")
    rows = load(path)
    row = find(rows, row_id)
    if row["status"] != "open":
        raise LedgerError(f"{row_id} is {row['status']}, not open")
    row["status"] = "void"
    row["void_reason"] = reason
    row["settled_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save(rows, path)
    return row


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _pct(value: object) -> str:
    return "     -" if value is None else f"{float(value) * 100:6.2f}%"


def _print(rows: list[dict]) -> None:
    if not rows:
        print("the ledger is empty")
        return
    print(f"{'id':7s} {'status':7s} {'kind':20s} {'venue':14s} {'size':>9s} "
          f"{'advert':>7s} {'forecast':>8s} {'realized':>8s} {'hold':>7s}")
    for r in rows:
        venue = r["venue"] if r["venue"] == "idle" else r["venue"][:12]
        hold = (f"{r['hold_minutes'] / 60:5.1f}h" if r.get("hold_minutes")
                else f"{r['forecast_horizon_min'] / 60:5.1f}h*")
        print(f"{r['id']:7s} {r['status']:7s} {r['kind'][:20]:20s} {venue:14s} "
              f"{r['size_usdg']:>9s} {_pct(r.get('advertised_apy')):>7s} "
              f"{_pct(r.get('forecast_net_apy')):>8s} "
              f"{_pct(r.get('realized_net_apy')):>8s} {hold:>7s}")
    live = open_rows(rows)
    committed = sum((_dec(r.get("size_usdg", 0)) for r in live), Decimal(0))
    print(f"\n{len(rows)} rows, {len(live)} open, {committed} USDG of "
          f"{_cfg()['sim_bankroll_usdg']} committed  (* = forecast horizon)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="print the ledger")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="print one row in full")
    p.add_argument("id")

    p = sub.add_parser("open", help="quote live, forecast, and write an open row")
    p.add_argument("--venue", required=True, help="address, or 'idle'")
    p.add_argument("--market-id", help="for a Morpho market")
    p.add_argument("--size", required=True, help="USDG")
    p.add_argument("--horizon", type=int, required=True, help="minutes")
    p.add_argument("--gross-apy", type=float,
                   help="the agent's own forecast of the gross rate, before costs")
    p.add_argument("--cycle")
    p.add_argument("--note")
    p.add_argument("--dry-run", action="store_true",
                   help="print the row and the caps it would break, write nothing")

    p = sub.add_parser("void", help="retire a row that cannot be settled honestly")
    p.add_argument("id")
    p.add_argument("--reason", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "list":
        rows = load()
        if args.status:
            rows = [r for r in rows if r["status"] == args.status]
        if args.json:
            print(json.dumps(rows, indent=1, sort_keys=True))
        else:
            _print(rows)
        return 0

    if args.cmd == "show":
        print(json.dumps(find(load(), args.id), indent=1, sort_keys=True))
        return 0

    if args.cmd == "open":
        try:
            row = open_allocation(args.venue, args.size, args.horizon, args.gross_apy,
                                  market_id=args.market_id, cycle=args.cycle,
                                  note=args.note, dry_run=args.dry_run)
        except (LedgerError, cost.CostError) as err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        print(json.dumps(row, indent=1, sort_keys=True))
        print(f"rpc {json.dumps(rpc.stats())}", file=sys.stderr)
        return 0

    if args.cmd == "void":
        row = void(args.id, args.reason)
        print(f"{row['id']} void: {row['void_reason']}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
