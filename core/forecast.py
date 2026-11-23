"""The forecast ledger: rate forecasts scored without a stake.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

This is the only writer of `journal/forecasts.jsonl`.

What Mel is learning is a rate: what a venue will pay over a horizon, against
what it advertised. `journal/allocations.jsonl` measures that only where
capital went, about two deployed rows a week, which is too few to say within
a season whether the agent forecasts better than a mechanical rule. A rate
forecast needs no capital. A row here forecasts the gross rate a position of
`max_allocation_usdg` would earn at one venue over one horizon. It is recorded
from a live quote, and settled later against the venue's own on-chain state
by the same arithmetic `core/settle.py` uses for real allocations. Nothing is
deployed and nothing is charged, so every rate on a forecast row is gross and
none of them is comparable with the allocation ledger's net numbers.
`core/score.py` reports the two in separate sections.

Two forecasters write here:

  baseline  core, mechanically, for every venue a quote touches: the board
            quote (`core/venues.py quote`, `record-baselines`) and the exit
            quote that settles a row. A baseline row has no model and carries
            `forecast_gross_apy: null`.
  agent     the cycle agent, for each venue it researched on a full cycle, at
            every horizon, before any allocation decision (`record`).

Every row, of either forecaster, carries the mechanical baselines read at the
same moment, because the advertised rate is too easy a number to beat:

  advertised    the venue's own spot rate, from the quote;
  trailing_24h  what the venue's own share price paid over about the last day
  trailing_7d   and the last week, compounded like every realized rate here.
                The endpoint forgets state after ten minutes, so the start
                price comes from the venue's own events, whose assets and
                shares pin the share price at a block that can no longer be
                read: Morpho `Supply` and `Withdraw`, ERC-4626 `Deposit` and
                `Withdraw`. The end price is the live state the row records.
                The anchor event must sit between 0.75 and 1.5 times the
                target age; otherwise the baseline is null and
                `baseline_notes` says why. A v3 pool has no share price, so
                both are null there;
  haircut       the advertised rate times the agent's own `forecast_haircut`
                for the kind in `strategy/risk.json`, as it stood at record
                time.

The agent's value is its error against the best of these, not against the
advertised rate.

Rules, and why:

  - One live row per (forecaster, venue, market_id, horizon). Rates are
    autocorrelated and venues share interest-rate models, so a venue
    re-forecast every cycle floods the sample with copies of one observation.
    A new forecast on a key with an open row is refused, never superseded.
  - Horizons are the ones the agent can allocate at: 1440, 4320 and 10080
    minutes.
  - A row is recorded only if `core/settle.py` could settle it: a kind it has
    maths for, and an entry state carrying every field that maths reads.
  - Every rate a row carries at entry, forecast or baseline, lies in
    (-100%, 1000%]. A venue whose own advertised or trailing rate falls
    outside that is moving with a price, not accruing a rate: on this chain,
    the Arcus 3x Long vaults, whose share price is leveraged BTC or HOOD.
    Their realized rate over a day is a price move compounded 365 times, and
    one such row outweighs every lending row in a mean error. They get no
    row, and the quote says why.
  - A row settles at the first `settle` after its horizon, against state read
    live at that moment, annualized over the actual hold. The realized gross
    is `settle.realized_gross` on a position of the row's notional size, so
    the exit is held to the same exitability check a real one gets: a Morpho
    market or vault that could not pay the position back at the horizon voids
    the row, with the reason. An exit state that cannot be read is retried
    every tick and voids `SETTLE_GRACE_MIN` after the horizon. A void is an
    absence, never a guess, and the score counts voids.
  - Errors are realized minus predicted, the sign `net_delta` has in the
    allocation score: negative means the predictor promised more than the
    venue paid.

Commands:
    python3 core/forecast.py record --venue 0x9d53 --market-id 0xc845 \
        --horizon 1440 4320 10080 --gross-apy 0.041 0.040 0.038 --note "..."
    python3 core/forecast.py record-baselines [--top N] [--rank-by depth|rate] \
        [--min-depth USDG] [--kind KIND] [--venue ADDR]
    python3 core/forecast.py due [--json]
    python3 core/forecast.py settle [--id f000001] [--dry-run] [--no-record] [--json]
    python3 core/forecast.py status [--json]
"""

from __future__ import annotations

import argparse
import calendar
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

from core import abi, ledger, rpc, settle, venues

FORECASTS_PATH = ROOT / "journal" / "forecasts.jsonl"
RISK_PATH = ROOT / "strategy" / "risk.json"

SECONDS_YEAR = 365 * 24 * 3600
FORECASTERS = ("agent", "baseline")
HORIZONS = (1440, 4320, 10080)
STATUSES = ("open", "settled", "void")
BASELINES = ("advertised", "trailing_24h", "trailing_7d", "haircut")
PREDICTORS = ("agent",) + BASELINES

# The kinds `settle.realized_gross` has maths for. Idle realizes zero by
# definition, so there is nothing to forecast; a v2 pair has no settlement
# maths, so a forecast on one could never settle.
SETTLEABLE = ("morpho_market", "erc4626_usdg_vault", "uniswap_v3_pool")

# The fields each kind's settlement maths reads from a recorded state. A
# Morpho row also needs the IRM's rate: without it the entry share price
# cannot be accrued to the entry block, and a stale entry price overstates
# every realized rate measured from it.
ENTRY_FIELDS = {
    "morpho_market": ("total_supply_assets", "total_supply_shares",
                      "total_borrow_assets", "last_update", "market_fee",
                      "borrow_rate_wad"),
    "erc4626_usdg_vault": ("share_price",),
    "uniswap_v3_pool": ("sqrt_price_x96", "fee_growth_0", "fee_growth_1",
                        "usdg_is_token0", "liquidity"),
}

TRAILING = {"trailing_24h": 86_400, "trailing_7d": 7 * 86_400}
TRAILING_BAND = (0.75, 1.5)            # accepted anchor age, as multiples of the target
BLOCK_RATE_SPAN = 1_000_000            # blocks between the two reads that time the chain

EVENTS = {
    "morpho_market": ("Supply(bytes32,address,address,uint256,uint256)",
                      "Withdraw(bytes32,address,address,address,uint256,uint256)"),
    "erc4626_usdg_vault": ("Deposit(address,address,uint256,uint256)",
                           "Withdraw(address,address,address,uint256,uint256)"),
}
V3_TRAILING = ("a v3 pool has no share price, and the fee growth a trailing rate "
               "would need is outside the ten-minute state window")

# An exit state that cannot be read is retried each tick; after this long past
# the horizon the row is void, because a settlement much later than the horizon
# measures a different forecast.
SETTLE_GRACE_MIN = 360

# `settle` starts no new venue after this many seconds and leaves the rest due
# for the next run. A venue already started runs to the end, and a vault read
# can take minutes, so this sits well under the session's 600-second tool
# limit (cycles 615 and 640 to 642 lost whole batches to it).
SETTLE_BUDGET_SECONDS = 240

# A row's `ts` is written after its live quote returns. Anything later than this
# after `entry_ts` is not a forecast made at entry, and `core/validate.py` fails it.
MAX_RECORD_LAG_SECONDS = 1_800

# A forecast outside this is a typo, not a belief: the board's absurd rates are
# dust, and a rate at or under -100% is a total loss no supply position can take.
# A baseline outside it says the venue is priced, not paid (see domain_problem).
MAX_FORECAST_APY = 10.0

# An agent forecast this far above the venue's own advertised rate is far
# more often a units slip than a belief: f000187 recorded `4.9` meaning 4.9%
# against 4.98% advertised, which stored as 490% and doubled the agent's
# mean error for a week. Above the line the agent must pass --confirm-extreme,
# which costs one flag exactly when it should pause anyway (Phil's guard for
# the same class of error).
EXTREME_MULTIPLE = 3.0
EXTREME_MARGIN = 0.5


def extreme_problem(gross: float, advertised: float | None) -> str | None:
    """Why `gross` needs --confirm-extreme against `advertised`, or None."""
    if advertised is None or not _finite(advertised) or float(advertised) < 0:
        return None
    line = max(EXTREME_MULTIPLE * float(advertised), float(advertised) + EXTREME_MARGIN)
    if float(gross) > line:
        return (f"forecast {gross} is above {line:.4f}, the extreme line for an "
                f"advertised rate of {float(advertised):.4f}. Rates are fractions: "
                f"4.9% is 0.049. If {gross} is really your belief, pass "
                "--confirm-extreme")
    return None

REALIZED_FIELDS = ("exit_block", "exit_ts", "exit_state", "hold_minutes",
                   "realized_gross_usdg", "realized_gross_apy", "errors",
                   "settle_method", "settle_detail", "settled_ts")
REQUIRED_FIELDS = ("id", "ts", "forecaster", "venue", "kind", "market_id",
                   "horizon_min", "size_usdg", "entry_block", "entry_ts",
                   "entry_state", "forecast_gross_apy", "baselines", "status")

ID_RE = re.compile(r"^f(\d{6,})$")


class ForecastError(Exception):
    """A forecast the rules refuse. Nothing reached the file."""


def _cfg() -> dict:
    return rpc.protected_config()


def _now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def _parse_iso(value: str) -> int:
    return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------

def load(path: Path = FORECASTS_PATH) -> list[dict]:
    """Every forecast row, oldest first."""
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
            raise ForecastError(f"{path.name} line {n} is not JSON: {err}") from err
    return rows


def save(rows: list[dict], path: Path = FORECASTS_PATH) -> None:
    """Rewrite the file atomically. Settlement edits rows in place."""
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def next_id(rows: list[dict]) -> str:
    highest = 0
    for row in rows:
        match = ID_RE.fullmatch(str(row.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"f{highest + 1:06d}"


def find(rows: list[dict], row_id: str) -> dict:
    for row in rows:
        if row.get("id") == row_id:
            return row
    raise ForecastError(f"no forecast with id {row_id!r}")


def venue_key(item: dict) -> str:
    """One venue, from a quote or a row: a Morpho market is its instance and
    its market id, anything else its address."""
    mid = item.get("market_id")
    base = str(item["venue"]).lower()
    return f"{base}:{str(mid).lower()}" if mid else base


def live_key(row: dict) -> tuple[str, str, int]:
    """What the one-live-row rule counts."""
    return (str(row["forecaster"]), venue_key(row), int(row["horizon_min"]))


def live(rows: list[dict]) -> dict[tuple, dict]:
    return {live_key(r): r for r in rows if r.get("status") == "open"}


def notional_usdg() -> str:
    """The position a forecast row describes: a full-size allocation."""
    return ledger.usdg_amount(_cfg()["max_allocation_usdg"])


def haircut_factors(path: Path | None = None) -> dict[str, float]:
    """The agent's `forecast_haircut` as it stands now. Read at record time
    and copied onto the row, so a later edit cannot rewrite a baseline."""
    try:
        data = json.loads((path or RISK_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for kind, value in (data.get("forecast_haircut") or {}).items():
        try:
            out[kind] = float(value)
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------
# What a row must be
# --------------------------------------------------------------------------

def entry_problem(quote: dict) -> str | None:
    """Why a quote cannot become a forecast row, or None. A row that could
    never settle is worse than no row."""
    kind = quote.get("kind")
    if kind == "idle":
        return "idle realizes zero by definition; there is nothing to forecast"
    if kind not in SETTLEABLE:
        return f"core/settle.py has no settlement maths for {kind}"
    if quote.get("error"):
        return f"the quote failed: {quote['error']}"
    state = quote.get("settle_state") or {}
    missing = [f for f in ENTRY_FIELDS[kind] if state.get(f) in (None, "", "None")]
    if missing:
        return (f"entry state has no {', '.join(missing)}, so core/settle.py "
                "could never settle this row")
    try:
        if kind == "erc4626_usdg_vault" and int(state["share_price"]) <= 0:
            return "the vault prices a share at zero"
        if kind == "morpho_market" and int(state["total_supply_shares"]) <= 0:
            return "the market has no supply shares; its share price is undefined"
    except (TypeError, ValueError) as err:
        return f"entry state does not parse: {err}"
    return None


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def domain_problem(baselines: dict) -> str | None:
    """Why a venue's own rates at entry say it is not a rate venue, or None.
    Judged on entry-time numbers only: a row dropped for its outcome would
    hide exactly the errors the ledger exists to count."""
    wild = {k: float(v) for k, v in baselines.items()
            if _finite(v) and not -1.0 < float(v) <= MAX_FORECAST_APY}
    if not wild:
        return None
    listed = ", ".join(f"{k} {v:+.2%}" for k, v in wild.items())
    return (f"{listed} at entry is outside (-100%, {MAX_FORECAST_APY:.0%}]: the venue "
            "moves with a price, not a rate, and one such row would outweigh "
            "every other row in a mean error")


def row_problems(row: dict, lag_seconds: int = MAX_RECORD_LAG_SECONDS) -> list[str]:
    """Every rule a row breaks, as it stands in the file. `core/validate.py`
    runs this over every row; the writer runs it before every write."""
    out: list[str] = []
    rid = row.get("id", "?")
    for field in REQUIRED_FIELDS:
        if field not in row:
            out.append(f"{rid} is missing {field}")
    if out:
        return out
    if not ID_RE.fullmatch(str(rid)):
        out.append(f"{rid} is not a forecast id")
    if row["forecaster"] not in FORECASTERS:
        out.append(f"{rid} forecaster {row['forecaster']!r} is not one of {FORECASTERS}")
    if row["kind"] not in SETTLEABLE:
        out.append(f"{rid} kind {row['kind']!r} cannot be settled")
    if row["horizon_min"] not in HORIZONS:
        out.append(f"{rid} horizon_min {row['horizon_min']} is not one of {HORIZONS}")
    if row["status"] not in STATUSES:
        out.append(f"{rid} status {row['status']!r}")
    if not ledger.AMOUNT_RE.fullmatch(str(row["size_usdg"])):
        out.append(f"{rid} size_usdg {row['size_usdg']!r} is not a decimal string in whole USDG")
    if (row["kind"] == "morpho_market") != bool(row.get("market_id")):
        out.append(f"{rid} market_id must be set for a Morpho market and only for one")

    forecast = row["forecast_gross_apy"]
    if row["forecaster"] == "baseline" and forecast is not None:
        out.append(f"{rid} is a baseline row carrying a forecast")
    if row["forecaster"] == "agent":
        if not _finite(forecast):
            out.append(f"{rid} is an agent row with no finite forecast_gross_apy")
        elif not -1.0 < float(forecast) <= MAX_FORECAST_APY:
            out.append(f"{rid} forecast_gross_apy {forecast} is outside (-1, {MAX_FORECAST_APY}]")

    base = row["baselines"]
    if not isinstance(base, dict) or set(base) != set(BASELINES):
        out.append(f"{rid} baselines must hold exactly {BASELINES}")
    else:
        for name, value in base.items():
            if value is not None and not _finite(value):
                out.append(f"{rid} baseline {name} is {value!r}, not a rate or null")
        problem = domain_problem(base)
        if problem:
            out.append(f"{rid}: {problem}")

    problem = entry_problem({"kind": row["kind"], "settle_state": row["entry_state"]})
    if problem and row["kind"] in SETTLEABLE:
        out.append(f"{rid}: {problem}")

    try:
        lag = _parse_iso(row["ts"]) - int(row["entry_ts"])
        if lag > lag_seconds:
            out.append(f"{rid} was written {lag / 60:.0f} minutes after its entry block. "
                       "A forecast is made at entry or it is not a forecast")
    except (TypeError, ValueError):
        out.append(f"{rid} ts {row['ts']!r} does not parse")

    if row["status"] == "open":
        for field in REALIZED_FIELDS:
            if row.get(field) is not None:
                out.append(f"{rid} is open but carries {field}. A forecast written "
                           "after the outcome is not a forecast")
    elif row["status"] == "settled":
        out += _settled_problems(row)
    elif not row.get("void_reason"):
        out.append(f"{rid} is void with no reason")
    return out


def _settled_problems(row: dict) -> list[str]:
    rid = row["id"]
    out = []
    for field in REALIZED_FIELDS:
        if field != "settle_detail" and row.get(field) is None:
            out.append(f"{rid} is settled but has no {field}")
    if out:
        return out
    exact = (int(row["exit_ts"]) - int(row["entry_ts"])) / 60
    if exact < int(row["horizon_min"]):
        out.append(f"{rid} settled after {exact:.1f} minutes, before its "
                   f"{row['horizon_min']}-minute horizon")
    if not ledger.AMOUNT_RE.fullmatch(str(row["realized_gross_usdg"])):
        out.append(f"{rid} realized_gross_usdg is not a decimal string in whole USDG")
        return out
    want = ledger.net_apy(row["size_usdg"], row["realized_gross_usdg"], exact)
    if abs(float(row["realized_gross_apy"]) - want) > 1e-9:
        out.append(f"{rid} realized_gross_apy {row['realized_gross_apy']} does not follow "
                   f"from {row['realized_gross_usdg']} USDG on {row['size_usdg']} over "
                   f"{exact:.2f} minutes ({want})")
    errors = row["errors"]
    if not isinstance(errors, dict) or set(errors) != set(PREDICTORS):
        out.append(f"{rid} errors must hold exactly {PREDICTORS}")
        return out
    for name, predicted in predictions(row).items():
        got = errors[name]
        if predicted is None:
            if got is not None:
                out.append(f"{rid} has an error for {name}, which predicted nothing")
        elif got is None or abs(float(got) - (float(row["realized_gross_apy"]) - predicted)) > 1e-12:
            out.append(f"{rid} errors.{name} is not realized minus predicted")
    return out


def predictions(row: dict) -> dict[str, float | None]:
    """Every predictor's number for a row: the agent's forecast, then each
    mechanical baseline."""
    out = {"agent": row.get("forecast_gross_apy")}
    out.update({k: (row.get("baselines") or {}).get(k) for k in BASELINES})
    return {k: (None if v is None else float(v)) for k, v in out.items()}


# --------------------------------------------------------------------------
# Baselines
# --------------------------------------------------------------------------

def price_now(quote: dict) -> float:
    """The share price a row records as its entry, in raw USDG per raw share.
    A Morpho price is accrued to the quote's own block, as `settle.py` accrues
    it, because the stored price only moves when someone touches the market."""
    state = quote["settle_state"]
    if quote["kind"] == "morpho_market":
        return settle._share_price(settle.accrue_morpho(state, int(quote["ts"])))
    unit = int(state.get("share_unit") or 0)
    if unit <= 0:
        raise settle.SettleError("the vault quote carries no share_unit")
    return int(state["share_price"]) / unit


def trailing_rate(price_from: float, price_to: float, seconds: float) -> float:
    """A compounded annual rate from two share prices, the convention every
    realized rate in this repo uses."""
    return (price_to / price_from) ** (SECONDS_YEAR / seconds) - 1


def trailing_from_anchor(quote: dict, name: str, anchor: dict | None,
                         now_price: float | None = None) -> tuple[float | None, dict]:
    """One trailing baseline from the chosen anchor event. Pure: the anchor
    is `{"block", "ts", "price", "assets"}`, already read."""
    target = TRAILING[name]
    if anchor is None:
        return None, {"reason": "no qualifying event near the target age"}
    if now_price is None:
        now_price = price_now(quote)
    seconds = int(quote["ts"]) - int(anchor["ts"])
    lo, hi = TRAILING_BAND[0] * target, TRAILING_BAND[1] * target
    if not lo <= seconds <= hi:
        return None, {"reason": f"the nearest event is {seconds / 3600:.1f} h before entry, "
                                f"outside {lo / 3600:.0f}-{hi / 3600:.0f} h",
                      "from_block": anchor["block"]}
    try:
        rate = trailing_rate(float(anchor["price"]), now_price, seconds)
    except (OverflowError, ZeroDivisionError):
        rate = math.inf
    if not math.isfinite(rate):
        return None, {"reason": "the share price moved too far to annualize",
                      "from_block": anchor["block"]}
    return rate, {"from_block": anchor["block"], "from_ts": anchor["ts"],
                  "window_seconds": seconds, "price_from": repr(float(anchor["price"])),
                  "price_to": repr(now_price),
                  "event_assets_usdg": venues.usdg_str(int(anchor["assets"])),
                  "source": "share price from the venue's own event, to the live entry state"}


def _event_price(kind: str, log: dict, floor_raw: int) -> tuple[float, int] | None:
    """(assets per share, assets) from one Supply/Withdraw/Deposit event, or
    None when the transfer is too small to price or does not decode."""
    data = log.get("data") or "0x"
    first = 0
    if kind == "morpho_market" and log["topics"][0] == abi.TOPICS[EVENTS[kind][1]]:
        first = 1                            # Morpho's Withdraw leads with the caller
    if len(data) < 2 + 64 * (first + 2):
        return None
    assets, shares = abi.decode_uint(data, first), abi.decode_uint(data, first + 1)
    if assets < floor_raw or shares <= 0:
        return None
    return assets / shares, assets


def _event_samples(quotes: list[dict], lo: int, hi: int) -> dict[str, list[tuple]]:
    """Every priceable event between two blocks, per venue key, as
    (block, price, assets)."""
    floor_raw = venues.VAULT_RATE_MIN_ASSETS_USDG * 10 ** _cfg()["quote_asset_decimals"]
    wanted = {venue_key(q) for q in quotes}
    out: dict[str, list[tuple]] = {}
    for kind in EVENTS:
        group = [q for q in quotes if q["kind"] == kind]
        if not group:
            continue
        topics: list = [[abi.TOPICS[sig] for sig in EVENTS[kind]]]
        if kind == "morpho_market":
            topics.append(sorted({q["market_id"].lower() for q in group}))
        addresses = sorted({q["venue"].lower() for q in group})
        for lg in rpc.scan_logs(lo, hi, addresses, topics, span=500_000):
            key = lg["address"].lower()
            if kind == "morpho_market":
                key = f"{key}:{lg['topics'][1].lower()}"
            if key not in wanted:
                continue
            priced = _event_price(kind, lg, floor_raw)
            if priced:
                out.setdefault(key, []).append((rpc.to_int(lg["blockNumber"]), *priced))
    return out


def _nearest(samples: list[tuple], block: int, per_second: float, target: int) -> tuple:
    """The sample whose estimated age is closest to the target, the larger
    transfer on a tie. Exact times are read for the winner only."""
    return min(samples, key=lambda s: (abs((block - s[0]) / per_second - target), -s[2]))


def _block_times(blocks: list[int]) -> dict[int, int]:
    """Block timestamps. `eth_getLogs` on this endpoint returns
    `blockTimestamp: 0x0`, so a log's time has to be read from its block."""
    got = rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in blocks])
    return {b: rpc.to_int(blk["timestamp"]) for b, blk in zip(blocks, got)
            if isinstance(blk, dict)}


def trailing_baselines(quotes: list[dict]) -> dict[str, dict[str, tuple]]:
    """`trailing_24h` and `trailing_7d` for each quote, keyed by venue key,
    each a (rate or None, note) pair. Reads the chain: one block-time read,
    one event scan per target and kind, one block-time batch for the anchors.
    A failed read nulls the baseline with the reason; it never fails the
    caller, because a row with a null baseline is worth more than no row."""
    out: dict[str, dict[str, tuple]] = {venue_key(q): {} for q in quotes}
    readable: list[tuple[dict, float]] = []
    for q in quotes:
        key = venue_key(q)
        if q["kind"] not in EVENTS:
            for name in TRAILING:
                out[key][name] = (None, {"reason": V3_TRAILING if q["kind"] == "uniswap_v3_pool"
                                         else f"no share price for {q['kind']}"})
            continue
        try:
            readable.append((q, price_now(q)))
        except (settle.SettleError, KeyError, TypeError, ValueError, ZeroDivisionError) as err:
            for name in TRAILING:
                out[key][name] = (None, {"reason": f"the entry share price is unreadable: {err}"})
    if not readable:
        return out

    def fail(name: str, reason: str) -> None:
        for q, _ in readable:
            out[venue_key(q)].setdefault(name, (None, {"reason": reason}))

    ref_block = max(int(q["block"]) for q, _ in readable)
    ref_ts = max(int(q["ts"]) for q, _ in readable)
    low_block = min(int(q["block"]) for q, _ in readable)
    try:
        then = _block_times([ref_block - BLOCK_RATE_SPAN])[ref_block - BLOCK_RATE_SPAN]
        per_second = BLOCK_RATE_SPAN / max(ref_ts - then, 1)
    except (rpc.RpcError, KeyError) as err:
        for name in TRAILING:
            fail(name, f"could not time the chain: {str(err)[:160]}")
        return out

    for name, target in TRAILING.items():
        lo = max(1, low_block - int(target * TRAILING_BAND[1] * per_second * 1.02))
        hi = ref_block - int(target * TRAILING_BAND[0] * per_second * 0.98)
        try:
            samples = _event_samples([q for q, _ in readable], lo, hi)
        except rpc.RpcError as err:
            fail(name, f"event scan failed: {str(err)[:160]}")
            continue
        picks: dict[str, tuple] = {}
        for q, now_price in readable:
            key = venue_key(q)
            found = samples.get(key) or []
            if not found:
                out[key][name] = (None, {"reason": f"no {' or '.join(s.split('(')[0] for s in EVENTS[q['kind']])} "
                                                   f"of at least {venues.VAULT_RATE_MIN_ASSETS_USDG} USDG "
                                                   f"in blocks {lo}..{hi}"})
                continue
            picks[key] = (q, now_price, _nearest(found, int(q["block"]), per_second, target))
        if not picks:
            continue
        try:
            stamps = _block_times(sorted({p[2][0] for p in picks.values()}))
        except rpc.RpcError as err:
            for key in picks:
                out[key][name] = (None, {"reason": f"could not time the anchor events: {str(err)[:160]}"})
            continue
        for key, (q, now_price, (block, price, assets)) in picks.items():
            if block not in stamps:
                out[key][name] = (None, {"reason": f"block {block} has no timestamp"})
                continue
            anchor = {"block": block, "ts": stamps[block], "price": price, "assets": assets}
            out[key][name] = trailing_from_anchor(q, name, anchor, now_price)
    return out


def baselines_for(quote: dict, trailing: dict[str, tuple] | None,
                  factors: dict[str, float]) -> tuple[dict, dict]:
    """The four mechanical baselines for one quote, and a note for each."""
    values: dict[str, float | None] = {}
    notes: dict[str, dict] = {}
    adv = quote.get("advertised_apy")
    adv = float(adv) if _finite(adv) else None
    values["advertised"] = adv
    notes["advertised"] = ({"source": quote.get("rate_source")} if adv is not None else
                           {"reason": quote.get("rate_error") or quote.get("error")
                            or "the quote carried no rate"})
    for name in TRAILING:
        value, note = (trailing or {}).get(name) or (None, {"reason": "not read"})
        values[name], notes[name] = value, note
    factor = factors.get(quote["kind"])
    if adv is None:
        values["haircut"], notes["haircut"] = None, {"reason": "no advertised rate to haircut"}
    elif factor is None:
        values["haircut"] = None
        notes["haircut"] = {"reason": f"strategy/risk.json has no forecast_haircut for {quote['kind']}"}
    else:
        values["haircut"] = adv * factor
        notes["haircut"] = {"factor": factor,
                            "source": f"strategy/risk.json forecast_haircut.{quote['kind']}"}
    return values, notes


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

def build_row(quote: dict, forecaster: str, horizon_min: int,
              forecast_gross_apy: float | None, baselines: dict, notes: dict,
              note: str | None = None, cycle: str | None = None,
              now_ts: float | None = None) -> dict:
    row = {
        "id": None,
        "ts": _now_iso(now_ts),
        "forecaster": forecaster,
        "venue": quote["venue"],
        "kind": quote["kind"],
        "market_id": quote.get("market_id"),
        "horizon_min": int(horizon_min),
        "size_usdg": notional_usdg(),
        "entry_block": int(quote["block"]),
        "entry_ts": int(quote["ts"]),
        "entry_state": quote.get("settle_state") or {},
        "entry_depth_usdg": quote.get("depth_usdg"),
        "forecast_gross_apy": None if forecast_gross_apy is None else float(forecast_gross_apy),
        "baselines": dict(baselines),
        "baseline_notes": dict(notes),
        "status": "open",
    }
    if quote.get("name"):
        row["name"] = quote["name"]
    if note:
        row["note"] = note
    if cycle:
        row["cycle"] = str(cycle)
    return row


def write(new: list[dict], path: Path = FORECASTS_PATH) -> dict:
    """Append the rows the rules accept; report the rest. The one-live-row
    rule is checked here, against the file as it is now."""
    rows = load(path)
    held = live(rows)
    recorded, refused = [], []
    for row in new:
        key = live_key(row)
        if key in held:
            refused.append({"forecaster": row["forecaster"], "venue": venue_key(row),
                            "horizon_min": row["horizon_min"],
                            "reason": f"{held[key]['id']} is still open for this venue and "
                                      "horizon; a live forecast is never superseded"})
            continue
        row = dict(row, id=next_id(rows))
        problems = row_problems(row)
        if problems:
            refused.append({"forecaster": row["forecaster"], "venue": venue_key(row),
                            "horizon_min": row["horizon_min"], "reason": "; ".join(problems)})
            continue
        rows.append(row)
        held[key] = row
        recorded.append(row)
    if recorded:
        save(rows, path)
    return {"recorded": recorded, "refused": refused}


def record_from_quote(quote: dict, horizons: list[int], gross_apys: list[float],
                      note: str, cycle: str | None = None,
                      path: Path = FORECASTS_PATH,
                      trailing: dict[str, tuple] | None = None,
                      now_ts: float | None = None,
                      confirm_extreme: bool = False) -> dict:
    """Agent rows from one live quote, one per horizon. `trailing` is read from
    the chain when not given, and `now_ts` is the clock; tests give both."""
    if not note or not note.strip():
        raise ForecastError("an agent forecast needs a note: what the number rests on")
    if len(horizons) != len(gross_apys) or not horizons:
        raise ForecastError("give one --gross-apy per --horizon, in the same order")
    for h in horizons:
        if h not in HORIZONS:
            raise ForecastError(f"horizon {h} is not one of {HORIZONS}")
    if len(set(horizons)) != len(horizons):
        raise ForecastError("a horizon appears twice")
    for g in gross_apys:
        if not _finite(g) or not -1.0 < float(g) <= MAX_FORECAST_APY:
            raise ForecastError(f"gross APY {g} is outside (-1, {MAX_FORECAST_APY}]; "
                                "rates are fractions, 0.04 is 4%")
    problem = entry_problem(quote)
    if problem:
        raise ForecastError(f"{venue_key(quote)}: {problem}")
    if trailing is None:
        trailing = trailing_baselines([quote]).get(venue_key(quote), {})
    values, notes = baselines_for(quote, trailing, haircut_factors())
    problem = domain_problem(values)
    if problem:
        raise ForecastError(f"{venue_key(quote)}: {problem}")
    if not confirm_extreme:
        for g in gross_apys:
            problem = extreme_problem(g, values.get("advertised"))
            if problem:
                raise ForecastError(f"{venue_key(quote)}: {problem}")
    new = [build_row(quote, "agent", h, g, values, notes, note=note, cycle=cycle,
                     now_ts=now_ts)
           for h, g in zip(horizons, gross_apys)]
    return write(new, path)


def record(venue: str, market_id: str | None, horizons: list[int],
           gross_apys: list[float], note: str, cycle: str | None = None,
           path: Path = FORECASTS_PATH, confirm_extreme: bool = False) -> dict:
    """Quote one venue live and record the agent's forecast at each horizon."""
    try:
        quote = ledger.live_quote(venue, market_id)
    except ledger.LedgerError as err:
        raise ForecastError(str(err)) from err
    return record_from_quote(quote, horizons, gross_apys, note, cycle, path,
                             confirm_extreme=confirm_extreme)


def record_baselines(quotes: list[dict], path: Path = FORECASTS_PATH,
                     trailing: dict[str, dict] | None = None,
                     now_ts: float | None = None) -> dict:
    """Baseline rows for every settleable quote, at every horizon whose key is
    free. Skips, with the reason, every quote that could never settle."""
    held = live(load(path))
    skipped, wanted = [], []
    for q in quotes:
        if q.get("kind") == "idle":
            continue
        problem = entry_problem(q)
        if problem:
            skipped.append({"venue": venue_key(q), "kind": q.get("kind"), "reason": problem})
            continue
        if all(("baseline", venue_key(q), h) in held for h in HORIZONS):
            continue
        wanted.append(q)
    if trailing is None:
        trailing = trailing_baselines(wanted) if wanted else {}
    factors = haircut_factors()
    new = []
    for q in wanted:
        values, notes = baselines_for(q, trailing.get(venue_key(q)), factors)
        problem = domain_problem(values)
        if problem:
            skipped.append({"venue": venue_key(q), "kind": q["kind"], "reason": problem})
            continue
        new += [build_row(q, "baseline", h, None, values, notes, now_ts=now_ts)
                for h in HORIZONS if ("baseline", venue_key(q), h) not in held]
    result = write(new, path)
    result["skipped"] = skipped
    result["already_live"] = sum(1 for q in quotes if q.get("kind") in SETTLEABLE
                                 for h in HORIZONS if ("baseline", venue_key(q), h) in held)
    return result


def void_recording_error(row_id: str, reason: str, path: Path = FORECASTS_PATH) -> dict:
    """OPERATOR ONLY. Retire a row whose recorded number was never the
    forecaster's belief (a units slip, a wrong venue). Not for a forecast that
    turned out badly: voiding a bad forecast is how a ledger lies about
    itself. The realized fields stay on the row so the record is complete;
    status void takes it out of every score."""
    if not reason or not reason.strip():
        raise ForecastError("a void needs a reason")
    rows = load(path)
    hit = [r for r in rows if r["id"] == row_id]
    if not hit:
        raise ForecastError(f"no forecast row {row_id}")
    row = hit[0]
    if row["status"] == "void":
        raise ForecastError(f"{row_id} is already void")
    row["status"] = "void"
    row["void_reason"] = f"recording error (operator): {reason.strip()}"
    save(rows, path)
    return row


# --------------------------------------------------------------------------
# Settling
# --------------------------------------------------------------------------

def due(rows: list[dict], now_ts: float | None = None) -> list[dict]:
    """Open rows whose horizon has passed."""
    now = time.time() if now_ts is None else now_ts
    return [r for r in rows if r.get("status") == "open"
            and now >= int(r["entry_ts"]) + int(r["horizon_min"]) * 60]


def settle_against(row: dict, quote: dict | None, now_ts: float | None = None) -> tuple[str, object]:
    """What one due row becomes against a live exit quote. Pure.

    Returns ("settled", fields), ("void", reason) or ("retry", reason). An
    exit the venue could not pay voids at once, because it is a fact about
    the horizon. An exit state that cannot be read is retried until
    `SETTLE_GRACE_MIN` past the horizon.
    """
    now = time.time() if now_ts is None else now_ts
    horizon_ts = int(row["entry_ts"]) + int(row["horizon_min"]) * 60
    overdue_min = (now - horizon_ts) / 60

    def unreadable(reason: str) -> tuple[str, str]:
        if overdue_min >= SETTLE_GRACE_MIN:
            return "void", f"{reason}; still unsettleable {overdue_min / 60:.1f} h past the horizon"
        return "retry", reason

    if quote is None:
        return unreadable("the venue did not quote at exit")
    if quote.get("error"):
        return unreadable(f"the exit quote failed: {quote['error']}")
    exit_state = quote.get("settle_state") or {}
    exit_block, exit_ts = int(quote["block"]), int(quote["ts"])
    if exit_ts < horizon_ts:
        return "retry", "the exit block is before the horizon"

    kind = row["kind"]
    if kind in ledger.LIQUIDITY_CHECKED_KINDS:
        free = ledger.free_liquidity(kind, exit_state)
        if free is None:
            return unreadable(f"exit state: {ledger.unverifiable_why(exit_state)}")
        multiple = Decimal(str(_cfg()["min_free_liquidity_multiple"]))
        need = Decimal(row["size_usdg"]) * multiple
        if free < need:
            sim = exit_state.get("exit_simulation")
            if isinstance(sim, dict) and not sim.get("ok"):
                why = (f"a simulated withdraw of {ledger.free_liquidity_amount(sim)} USDG "
                       f"reverted for every holder tried "
                       f"({'; '.join(sim.get('errors') or [sim.get('note', '')])[:300]})")
            else:
                why = (f"free liquidity at exit is {free} USDG, under "
                       f"min_free_liquidity_multiple {multiple} x {row['size_usdg']}")
            return "void", f"not exitable at the horizon: {why}"

    position = {k: row[k] for k in ("id", "kind", "size_usdg", "entry_ts", "entry_state")}
    try:
        outcome = settle.realized_gross(position, exit_state, exit_ts)
    except settle.SettleError as err:
        return unreadable(str(err))

    hold = (exit_ts - int(row["entry_ts"])) / 60
    realized = ledger.net_apy(row["size_usdg"], outcome["gross_usdg"], hold)
    errors = {k: (None if v is None else realized - v) for k, v in predictions(row).items()}
    return "settled", {"exit_block": exit_block, "exit_ts": exit_ts,
                       "exit_state": exit_state, "hold_minutes": round(hold, 2),
                       "realized_gross_usdg": outcome["gross_usdg"],
                       "realized_gross_apy": realized, "errors": errors,
                       "settle_method": outcome["method"],
                       "settle_detail": outcome["detail"]}


def apply(row: dict, action: str, payload: object, now_ts: float | None = None) -> dict:
    """Close a row in place. Returns a one-line summary of what happened."""
    if action == "settled":
        row.update(payload)                           # type: ignore[arg-type]
        row["status"] = "settled"
        row["settled_ts"] = _now_iso(now_ts)
    elif action == "void":
        row["status"] = "void"
        row["void_reason"] = str(payload)
        row["settled_ts"] = _now_iso(now_ts)
    summary = {"id": row["id"], "forecaster": row["forecaster"], "venue": venue_key(row),
               "kind": row["kind"], "horizon_min": row["horizon_min"], "action": action}
    if action == "settled":
        summary.update({"hold_minutes": row["hold_minutes"],
                        "realized_gross_apy": row["realized_gross_apy"],
                        "errors": row["errors"]})
    else:
        summary["reason"] = str(payload)
    return summary


def exit_quotes(targets: list[dict]) -> tuple[dict[str, dict], str | None]:
    """Live quotes for the venues of `targets`, in one board read, with a
    vault withdraw simulated at the size the exit check needs."""
    keys = {venue_key(r) for r in targets}
    record = [r for r in venues.load() if r.get("kind") in SETTLEABLE
              and venues._key(r) in keys]
    if not record:
        return {}, "none of the due venues is in journal/venues.jsonl"
    size = max(Decimal(r["size_usdg"]) for r in targets)
    exit_usdg = float(size * Decimal(str(_cfg()["min_free_liquidity_multiple"])))
    # A holder whose withdraw passed when a row was recorded is tried before
    # the depositor scan; see `venues.exit_simulation`.
    holders: dict[str, list[str]] = {}
    for r in targets:
        h = ((r.get("entry_state") or {}).get("exit_simulation") or {}).get("holder")
        if h:
            holders.setdefault(r["venue"].lower(), []).append(h)
    try:
        quotes = venues.quote(record, include_idle=False, exit_usdg=exit_usdg,
                              exit_holders=holders)
    except (rpc.RpcError, venues.VenueError) as err:
        return {}, f"the exit quote failed: {str(err)[:200]}"
    return {venue_key(q): q for q in quotes}, None


def settle_due(path: Path = FORECASTS_PATH, row_id: str | None = None,
               dry_run: bool = False, rebaseline: bool = True,
               now_ts: float | None = None, quoter=None,
               budget_seconds: float = SETTLE_BUDGET_SECONDS, clock=time.monotonic) -> dict:
    """Settle due rows one venue at a time, each against one live read of
    that venue, and write each venue's rows before reading the next. Then
    record the venue's next baseline rows from that same read: the exit
    quote of a settled row is a live quote like any other, so the chain of
    baseline rows on a venue runs back to back instead of waiting for a full
    cycle.

    A run killed partway keeps every venue it finished. Past
    `budget_seconds` no new venue starts; the rows left are reported as
    `deferred` and stay due for the next run, unsettled rather than guessed.
    `quoter` reads the exit quotes; tests hand in recorded ones."""
    rows = load(path)
    if row_id:
        targets = [find(rows, row_id)]
        if targets[0]["status"] != "open":
            raise ForecastError(f"{row_id} is {targets[0]['status']}, not open")
    else:
        targets = due(rows, now_ts)
    if not targets:
        return {"results": [], "baselines": None, "deferred": []}

    by_venue: dict[str, list[str]] = {}
    for row in targets:
        by_venue.setdefault(venue_key(row), []).append(row["id"])

    started = clock()
    results: list[dict] = []
    recorded: list[dict] = []
    deferred: list[str] = []
    for n, ids in enumerate(by_venue.values()):
        if n and clock() - started >= budget_seconds:
            deferred += ids
            continue
        if not dry_run:
            # The previous venue's baselines were appended to the file since.
            rows = load(path)
        group = [find(rows, i) for i in ids]
        quotes, failure = (quoter or exit_quotes)(group)
        done = []
        for row in group:
            quote = quotes.get(venue_key(row))
            if quote is None and failure:
                quote = {"error": failure}
            action, payload = settle_against(row, quote, now_ts)
            done.append(apply(row if not dry_run else dict(row), action, payload, now_ts))
        results += done
        if dry_run or all(r["action"] == "retry" for r in done):
            continue
        save(rows, path)
        if rebaseline and quotes:
            fresh = [q for k, q in quotes.items() if k in {r["venue"] for r in done}]
            if fresh:
                recorded.append(record_baselines(fresh, path, now_ts=now_ts))

    out = {"results": results, "baselines": _merged(recorded), "deferred": deferred}
    if dry_run:
        out["dry_run"] = True
    return out


def _merged(parts: list[dict]) -> dict | None:
    """One `record_baselines` result from the per-venue ones."""
    if not parts:
        return None
    out: dict = {"recorded": [], "refused": [], "skipped": [], "already_live": 0}
    for part in parts:
        for k in ("recorded", "refused", "skipped"):
            out[k] += part.get(k) or []
        out["already_live"] += part.get("already_live", 0)
    return out


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

def status(rows: list[dict], now_ts: float | None = None) -> dict:
    """Counts, not scores: `core/score.py` owns the statistics."""
    now = time.time() if now_ts is None else now_ts
    table: dict[str, dict[str, dict[str, int]]] = {}
    for r in rows:
        cell = table.setdefault(r["forecaster"], {}).setdefault(
            r["kind"], {"open": 0, "settled": 0, "void": 0, "due": 0})
        cell[r["status"]] = cell.get(r["status"], 0) + 1
    for r in due(rows, now):
        table[r["forecaster"]][r["kind"]]["due"] += 1
    opened = [r for r in rows if r.get("status") == "open"]
    upcoming = sorted(int(r["entry_ts"]) + int(r["horizon_min"]) * 60 for r in opened)
    return {"rows": len(rows),
            "by_status": {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES},
            "table": table,
            "venues_live": len({venue_key(r) for r in opened}),
            "due_now": len(due(rows, now)),
            "next_due": _now_iso(upcoming[0]) if upcoming else None,
            "first_ts": rows[0]["ts"] if rows else None,
            "last_ts": rows[-1]["ts"] if rows else None}


def _print_status(data: dict) -> None:
    print(f"{data['rows']} forecast rows, {data['by_status']['open']} open, "
          f"{data['by_status']['settled']} settled, {data['by_status']['void']} void, "
          f"{data['due_now']} due now, next due {data['next_due'] or '-'}")
    if not data["table"]:
        return
    print(f"\n{'forecaster':10s} {'kind':20s} {'open':>5s} {'settled':>8s} {'void':>5s} {'due':>4s}")
    for forecaster in sorted(data["table"]):
        for kind, c in sorted(data["table"][forecaster].items()):
            print(f"{forecaster:10s} {kind:20s} {c['open']:>5d} {c['settled']:>8d} "
                  f"{c['void']:>5d} {c['due']:>4d}")
    print(f"\n{data['venues_live']} venues hold a live row")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _pct(value: object) -> str:
    return "      -" if value is None else f"{float(value) * 100:+6.2f}%"


def _print_written(result: dict) -> None:
    for row in result["recorded"]:
        b = row["baselines"]
        print(f"{row['id']}  {row['forecaster']:8s} {venue_key(row)[:30]:30s} "
              f"{row['horizon_min']:>5d}m  forecast {_pct(row['forecast_gross_apy'])}  "
              f"adv {_pct(b['advertised'])}  t24h {_pct(b['trailing_24h'])}  "
              f"t7d {_pct(b['trailing_7d'])}  haircut {_pct(b['haircut'])}")
    for r in result["refused"]:
        print(f"refused  {r['forecaster']:8s} {r['venue'][:30]:30s} {r['horizon_min']:>5d}m  "
              f"{r['reason']}")
    for s in result.get("skipped") or []:
        print(f"skipped  {s['venue'][:40]:40s} {s['reason'][:90]}")


def _jsonable(result: dict) -> dict:
    out = dict(result)
    out["recorded"] = [r["id"] for r in result.get("recorded", [])]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("record", help="the agent's forecast for one venue, from a live quote")
    p.add_argument("--venue", required=True, help="address, a prefix is enough")
    p.add_argument("--market-id", help="for a Morpho market")
    p.add_argument("--horizon", type=int, nargs="+", required=True,
                   help=f"minutes, any of {HORIZONS}")
    p.add_argument("--gross-apy", type=float, nargs="+", required=True,
                   help="the gross rate you believe, one per horizon, as a fraction")
    p.add_argument("--note", required=True, help="what the number rests on")
    p.add_argument("--cycle")
    p.add_argument("--confirm-extreme", action="store_true",
                   help="record a forecast far above the advertised rate on purpose")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("void-recording-error",
                       help="OPERATOR ONLY: retire a row that never held the forecaster's belief")
    p.add_argument("--id", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("record-baselines", help="quote venues live and record baseline rows")
    p.add_argument("--kind")
    p.add_argument("--venue")
    p.add_argument("--top", type=int, default=0)
    p.add_argument("--rank-by", choices=("depth", "rate"), default="depth")
    p.add_argument("--min-depth", type=float, default=0.0)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("due", help="open rows past their horizon")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("settle", help="settle due rows against live state")
    p.add_argument("--id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-record", action="store_true",
                   help="do not record the next baseline rows from the exit quotes")
    p.add_argument("--budget-seconds", type=float, default=SETTLE_BUDGET_SECONDS,
                   help="start no new venue after this long; the rest stay due")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="counts by forecaster, kind and status")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    try:
        if args.cmd == "record":
            result = record(args.venue, args.market_id, args.horizon, args.gross_apy,
                            args.note, args.cycle, confirm_extreme=args.confirm_extreme)
            if args.json:
                print(json.dumps(_jsonable(result), indent=1))
            else:
                _print_written(result)
            print(f"rpc {json.dumps(rpc.stats())}", file=sys.stderr)
            return 0 if result["recorded"] else 2

        if args.cmd == "void-recording-error":
            row = void_recording_error(args.id, args.reason)
            print(f"{row['id']} void: {row['void_reason']}")
            return 0

        if args.cmd == "record-baselines":
            quotes = venues.board(kind=args.kind, venue=args.venue, top=args.top,
                                  rank_by=args.rank_by, min_depth=args.min_depth)
            result = record_baselines(quotes)
            if args.json:
                print(json.dumps(_jsonable(result), indent=1))
            else:
                _print_written(result)
                print(f"\n{len(result['recorded'])} recorded, {result['already_live']} "
                      f"already live, {len(result['skipped'])} skipped")
            print(f"rpc {json.dumps(rpc.stats())}", file=sys.stderr)
            return 0

        rows = load()
        if args.cmd == "due":
            pending = due(rows)
            if args.json:
                print(json.dumps([{k: r[k] for k in ("id", "forecaster", "venue", "market_id",
                                                     "kind", "horizon_min", "entry_ts")}
                                  for r in pending], indent=1))
            else:
                now = time.time()
                for r in pending:
                    late = (now - int(r["entry_ts"])) / 60 - int(r["horizon_min"])
                    print(f"{r['id']}  {r['forecaster']:8s} {venue_key(r)[:40]:40s} "
                          f"{r['horizon_min']:>5d}m  due {late:.0f} min ago")
                print(f"{len(pending)} of {sum(1 for r in rows if r['status'] == 'open')} "
                      "open forecast rows are due")
            return 0

        if args.cmd == "settle":
            out = settle_due(row_id=args.id, dry_run=args.dry_run,
                             rebaseline=not args.no_record,
                             budget_seconds=args.budget_seconds)
            if args.json:
                if out.get("baselines"):
                    out["baselines"] = _jsonable(out["baselines"])
                print(json.dumps(out, indent=1, sort_keys=True))
            else:
                if not out["results"]:
                    print("no forecast is due")
                for r in out["results"]:
                    if r["action"] == "settled":
                        e = r["errors"]
                        print(f"{r['id']}  settled {r['venue'][:30]:30s} {r['horizon_min']:>5d}m  "
                              f"realized {_pct(r['realized_gross_apy'])}  err agent "
                              f"{_pct(e['agent'])} adv {_pct(e['advertised'])} "
                              f"t24h {_pct(e['trailing_24h'])} t7d {_pct(e['trailing_7d'])} "
                              f"haircut {_pct(e['haircut'])}")
                    else:
                        print(f"{r['id']}  {r['action']:7s} {r['venue'][:30]:30s} "
                              f"{r['horizon_min']:>5d}m  {r['reason'][:160]}")
                if out.get("baselines"):
                    b = out["baselines"]
                    print(f"next baselines from the exit quotes: {len(b['recorded'])} recorded")
                if out["deferred"]:
                    print(f"{len(out['deferred'])} due rows left for the next run, past the "
                          f"{args.budget_seconds:g}s budget: {' '.join(out['deferred'])}")
            print(f"rpc {json.dumps(rpc.stats())}", file=sys.stderr)
            return 0

        if args.cmd == "status":
            data = status(rows)
            if args.json:
                print(json.dumps(data, indent=1, sort_keys=True))
            else:
                _print_status(data)
            return 0
    except (ForecastError, ledger.LedgerError) as err:
        print(f"refused: {err}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
