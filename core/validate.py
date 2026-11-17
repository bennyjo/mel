"""Offline tripwires. What must be true of this repo before anything runs.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

This is the file that makes the honest-simulation rules enforceable rather than
aspirational. It reads the repo, never the chain, so CI can run it on every
push, and it fails loudly rather than warning quietly.

It checks:

  - real execution is off, the dormant real caps are still small, and
    `core/real.py` independently agrees the door is locked;
  - `config/protected.json` parses, is complete, and every cap is inside the
    hard ceilings defined below. The ceilings live here on purpose: raising a
    cap therefore takes an edit to a protected file, which the boundary guard
    can see;
  - every JSON file under `strategy/` parses, and any cap the agent restates
    there is tighter than the operator's, never looser;
  - every row of `journal/allocations.jsonl` obeys the schema, the
    string-amount rule, and every cap - replayed as of the moment it was
    written, not against today's book;
  - no open row carries a realized field, and every settled row's realized
    numbers follow arithmetically from its own amounts;
  - every row of `journal/forecasts.jsonl` obeys `core/forecast.py`'s own
    schema: no realized field while open, no forecast written more than
    `MAX_RECORD_LAG_SECONDS` after its entry block, every settled row's
    realized rate and errors recomputing from its own numbers, and never two
    live rows on one (forecaster, venue, market, horizon), including a row
    written while an earlier one on its key was still open;
  - `journal/venues.jsonl` is coherent, and every Morpho market id still
    reproduces from the keccak of its five stored parameter words;
  - `strategy/experiments.jsonl`, the agent's experiment ledger, parses and
    folds (`core/experiments.py`): at most three experiments open, every open
    experiment's commit an ancestor of HEAD touching only one or two
    `strategy/` files, no lever outside `strategy/`, and no experiment commit
    loosening a cap in `strategy/risk.json`;
  - the ABI selector table recomputes from its own signatures;
  - every Python file under `core/` and `strategy/` compiles.

Commands:
    python3 core/validate.py
    python3 core/validate.py --json
"""

from __future__ import annotations

import argparse
import json
import py_compile
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import abi, experiments, forecast, ledger, rpc
from core import real as real_leg

# Ceilings on what the config may say. A cap in `config/protected.json` may be
# tightened freely; loosening one past a ceiling here is a change to protected
# core, which is exactly the review the boundary guard exists to force. The
# numbers are set at roughly four times today's caps: enough headroom for the
# operator to scale the experiment, not enough for a runaway.
CEILINGS = {
    "sim_bankroll_usdg": ("<=", 10_000.0),
    "max_allocation_usdg": ("<=", 1_000.0),
    "max_open_allocations": ("<=", 24),
    "max_venue_share": ("<=", 0.5),
    "max_new_allocations_per_cycle": ("<=", 8),
    "min_hold_minutes": (">=", 60),
    "min_expected_net_bps": (">=", 5.0),
    "min_free_liquidity_multiple": (">=", 1.0),
}
REAL_CEILINGS = {
    "max_allocation_usdg": ("<=", 5.0),
    "daily_deploy_cap_usdg": ("<=", 20.0),
    "max_open_allocations": ("<=", 3),
}

# Rows opened before the vault idle-balance field existed cannot verify it;
# the replay notes them instead of failing (a00008, opened 2026-09-17 01:18).
VAULT_IDLE_RULE_TS = "2026-09-17T09:00:00Z"

# Caps the agent may restate in `strategy/risk.json`. It may only move each one
# in the direction that risks less.
TIGHTEN_ONLY = {
    "max_allocation_usdg": "<=", "max_open_allocations": "<=",
    "max_venue_share": "<=", "max_new_allocations_per_cycle": "<=",
    "min_hold_minutes": ">=", "min_expected_net_bps": ">=",
}

REQUIRED_CONFIG = ("chain_id", "rpc_url", "quote_asset", "quote_asset_address",
                   "quote_asset_decimals", "sim_bankroll_usdg",
                   "max_allocation_usdg", "max_open_allocations",
                   "max_venue_share", "max_new_allocations_per_cycle",
                   "min_hold_minutes", "min_expected_net_bps",
                   "min_free_liquidity_multiple", "loss_mark_after_horizon_minutes",
                   "real_execution_enabled", "real")


class Report:
    """Failures stop the run. Notes are things an operator should read."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []
        self.checks = 0

    def check(self, ok: bool, message: str) -> bool:
        self.checks += 1
        if not ok:
            self.failures.append(message)
        return ok

    def note(self, message: str) -> None:
        self.notes.append(message)

    def as_dict(self) -> dict:
        return {"ok": not self.failures, "checks": self.checks,
                "failures": self.failures, "notes": self.notes}


def _compare(value: float, op: str, limit: float) -> bool:
    return value <= limit if op == "<=" else value >= limit


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def check_config(rep: Report) -> dict:
    path = ROOT / "config" / "protected.json"
    if not rep.check(path.exists(), "config/protected.json is missing"):
        return {}
    try:
        cfg = json.loads(path.read_text())
    except json.JSONDecodeError as err:
        rep.check(False, f"config/protected.json is not JSON: {err}")
        return {}

    for key in REQUIRED_CONFIG:
        rep.check(key in cfg, f"config/protected.json has no {key}")
    if any(k not in cfg for k in REQUIRED_CONFIG):
        return cfg

    rep.check(cfg["real_execution_enabled"] is False,
              "real_execution_enabled must be false. Real allocation is an operator "
              "act and there are no Olas registries on chain 4663")
    rep.check(cfg["chain_id"] == 4663, f"chain_id is {cfg['chain_id']}, not 4663")
    rep.check(str(cfg["rpc_url"]).startswith("https://"),
              "rpc_url is not https")
    rep.check(cfg["quote_asset_decimals"] == 6,
              f"quote_asset_decimals is {cfg['quote_asset_decimals']}; USDG has 6")
    rep.check(len(str(cfg["quote_asset_address"])) == 42,
              "quote_asset_address is not a 20-byte address")

    for key, (op, limit) in CEILINGS.items():
        rep.check(_compare(float(cfg[key]), op, limit),
                  f"{key} is {cfg[key]}; the hard ceiling in core/validate.py "
                  f"is {op} {limit}")
    rep.check(float(cfg["max_allocation_usdg"]) <= float(cfg["sim_bankroll_usdg"]),
              "max_allocation_usdg is larger than the whole bankroll")

    real = cfg.get("real") or {}
    for key, (op, limit) in REAL_CEILINGS.items():
        if rep.check(key in real, f"config real block has no {key}"):
            rep.check(_compare(float(real[key]), op, limit),
                      f"real.{key} is {real[key]}; the ceiling is {op} {limit}")
    rep.check(isinstance(real.get("allowed_venues"), list),
              "real.allowed_venues must be a list")
    if real.get("allowed_venues"):
        rep.note(f"real.allowed_venues names {len(real['allowed_venues'])} venues "
                 "while real execution is off")

    # The config says real execution is off; this asks the module that would do
    # it whether it agrees. Two different codepaths reaching the same answer is
    # what makes the door locked rather than merely labelled.
    stops = [b["gate"] for b in real_leg.blockers(cfg, env={}) if b["blocked"]]
    rep.check("real_execution_enabled" in stops,
              "core/real.py does not report real execution as blocked, but "
              "config/protected.json says it is off")
    rep.check(not real_leg.ready(cfg, env={}),
              "core/real.py reports real execution ready. Nothing may touch "
              "funds on chain 4663")
    rep.note(f"real execution dormant; blocked on {', '.join(stops)}")
    return cfg


# --------------------------------------------------------------------------
# Strategy
# --------------------------------------------------------------------------

def check_strategy(rep: Report, cfg: dict) -> None:
    strategy = ROOT / "strategy"
    if not strategy.exists():
        rep.note("strategy/ does not exist yet")
        return
    files = sorted(strategy.glob("*.json"))
    if not files:
        rep.note("strategy/ holds no JSON yet")
    for path in files:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            rep.check(False, f"strategy/{path.name} is not JSON: {err}")
            continue
        rep.check(isinstance(data, dict),
                  f"strategy/{path.name} is not a JSON object")
        if not isinstance(data, dict):
            continue
        if path.name == "venue-filters.json" and "min_free_liquidity_multiple" in data:
            rep.check(float(data["min_free_liquidity_multiple"])
                      >= float(cfg["min_free_liquidity_multiple"]),
                      f"strategy/venue-filters.json min_free_liquidity_multiple is "
                      f"{data['min_free_liquidity_multiple']}, looser than the "
                      f"protected floor {cfg['min_free_liquidity_multiple']}")
        if path.name != "risk.json":
            continue
        for key, op in TIGHTEN_ONLY.items():
            if key not in data or key not in cfg:
                continue
            rep.check(_compare(float(data[key]), op, float(cfg[key])),
                      f"strategy/risk.json {key} is {data[key]}, which is looser "
                      f"than the protected cap {cfg[key]}. The agent may tighten "
                      "a cap, never loosen one")


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------

def _open_at(earlier: list[dict], when: str) -> list[dict]:
    """The book as it stood when `when` was written: the rows already in the
    file, each marked open or closed as it stood at that moment. Replaying
    against today's book would fail rows that were legal when written.

    "Already in the file" is file order, not timestamp order, because two rows
    written in the same second carry the same timestamp and each still has to
    see the other.
    """
    out = []
    for row in earlier:
        closed = str(row.get("settled_ts") or "")
        out.append(dict(row, status="open" if (not closed or closed > when)
                        else row.get("status", "settled")))
    return out


def check_ledger(rep: Report, cfg: dict, path: Path | None = None) -> list[dict]:
    path = path or ledger.LEDGER_PATH
    if not path.exists():
        rep.note(f"{path.name} does not exist yet; nothing has been allocated")
        return []
    try:
        rows = ledger.load(path)
    except ledger.LedgerError as err:
        rep.check(False, str(err))
        return []

    seen: set[str] = set()
    for index, row in enumerate(rows):
        rid = row.get("id", "?")
        rep.check(rid not in seen, f"{rid} appears twice in the ledger")
        seen.add(rid)
        rep.check(row.get("status") in ledger.STATUSES,
                  f"{rid} has status {row.get('status')!r}")

        for field in ledger.AMOUNT_FIELDS:
            value = row.get(field)
            if value is None:
                continue
            rep.check(isinstance(value, str) and ledger.AMOUNT_RE.fullmatch(value),
                      f"{rid} {field} is {value!r}: every USDG amount is a decimal "
                      "string in whole USDG, never a raw integer and never a float")

        if row.get("status") == "open":
            for field in ledger.REALIZED_FIELDS:
                rep.check(row.get(field) is None,
                          f"{rid} is open but carries {field}. A forecast written "
                          "after the outcome is not a forecast")

        as_of = _open_at(rows[:index], str(row.get("ts", "")))
        opened = {k: v for k, v in row.items()
                  if k not in ledger.REALIZED_FIELDS and k != "void_reason"}
        opened["status"] = "open"
        for problem in ledger.cap_violations(as_of, opened, cfg):
            if "already in the ledger" in problem:
                continue
            if row.get("status") == "void":
                rep.note(f"{rid} is void and would break a cap today: {problem}")
                continue
            if ("cannot be verified" in problem and row.get("kind") == "erc4626_usdg_vault"
                    and str(row.get("ts", "")) < VAULT_IDLE_RULE_TS):
                rep.note(f"{rid} predates the vault idle-balance field; its exit "
                         "is checked at settlement instead")
                continue
            rep.check(False, f"{rid} broke a cap when it was written: {problem}")

        if row.get("status") in ledger.SCORED_STATUSES:
            check_settled(rep, row, cfg)
        if row.get("status") == "marked":
            rep.check(bool(row.get("mark_reason")), f"{rid} is marked with no reason")
        if row.get("status") == "void":
            rep.check(bool(row.get("void_reason")), f"{rid} is void with no reason")

    voids = sum(1 for r in rows if r.get("status") == "void")
    if voids:
        rep.note(f"{voids} of {len(rows)} rows are void. A void is an absence, not "
                 "a zero; a strategy cannot improve by voiding its losers")
    return rows


def check_settled(rep: Report, row: dict, cfg: dict) -> None:
    rid = row["id"]
    for field in ("exit_block", "exit_ts", "exit_cost_usdg", "realized_usdg",
                  "realized_net_apy", "settled_ts", "hold_minutes"):
        if not rep.check(row.get(field) is not None,
                         f"{rid} is settled but has no {field}"):
            return
    try:
        size = Decimal(row["size_usdg"])
        gross = Decimal(row["realized_gross_usdg"])
        entry = Decimal(row["entry_cost_usdg"])
        exit_cost = Decimal(row["exit_cost_usdg"])
        net = Decimal(row["realized_usdg"])
    except (InvalidOperation, KeyError) as err:
        rep.check(False, f"{rid} settled amounts do not parse: {err}")
        return

    rep.check(net == gross - entry - exit_cost,
              f"{rid} realized_usdg {net} is not gross {gross} less the round trip "
              f"{entry + exit_cost}. Costs must be charged, always")
    hold = float(row["hold_minutes"])
    rep.check(hold >= float(cfg["min_hold_minutes"]),
              f"{rid} settled after {hold:.1f} minutes, under min_hold_minutes "
              f"{cfg['min_hold_minutes']}")
    rep.check(abs(hold - (int(row["exit_ts"]) - int(row["entry_ts"])) / 60) < 0.02,
              f"{rid} hold_minutes does not match its own entry and exit blocks")
    # The writer annualizes over the exact hold and stores hold_minutes to
    # 0.01; recompute from the same exact hold, or a seven-day row misses by
    # 3e-8 and every deployed settlement fails (a00004, 2026-09-17).
    exact = (int(row["exit_ts"]) - int(row["entry_ts"])) / 60
    want = ledger.net_apy(size, net, exact)
    rep.check(abs(float(row["realized_net_apy"]) - want) < 1e-9,
              f"{rid} realized_net_apy {row['realized_net_apy']} does not follow "
              f"from {net} USDG on {size} over {exact:.4f} minutes ({want})")
    if row.get("advertised_apy") is not None:
        rep.check(float(row["realized_net_apy"]) != float(row["advertised_apy"])
                  or float(row["advertised_apy"]) == 0.0,
                  f"{rid} realized_net_apy is exactly the advertised rate. An "
                  "advertised number is never a realized outcome")
    if row["kind"] == "idle":
        rep.check(net == 0 and gross == 0,
                  f"{rid} is idle and realized {net} USDG. Idle earns nothing and "
                  "costs nothing")


# --------------------------------------------------------------------------
# The forecast ledger
# --------------------------------------------------------------------------

def check_forecasts(rep: Report, path: Path | None = None) -> list[dict]:
    """Every forecast row against the writer's own rules, then the one-live-row
    rule replayed in file order: a row on a key must be written after every
    earlier row on that key closed. A superseded forecast is a forecast
    rewritten once the outcome began to show."""
    path = path or forecast.FORECASTS_PATH
    if not path.exists():
        rep.note(f"{path.name} does not exist yet; no rate forecast has been recorded")
        return []
    try:
        rows = forecast.load(path)
    except forecast.ForecastError as err:
        rep.check(False, str(err))
        return []

    seen: set[str] = set()
    last = 0
    history: dict[tuple, list[dict]] = {}
    for row in rows:
        problems = forecast.row_problems(row)
        rep.check(not problems, "forecasts.jsonl: " + "; ".join(problems))
        if any("is missing" in p for p in problems):
            continue
        rid = str(row["id"])
        rep.check(rid not in seen, f"forecast {rid} appears twice")
        seen.add(rid)
        match = forecast.ID_RE.fullmatch(rid)
        if match:
            rep.check(int(match.group(1)) > last,
                      f"forecast {rid} follows a higher id; rows are appended in id order")
            last = max(last, int(match.group(1)))
        key = forecast.live_key(row)
        for earlier in history.get(key, []):
            if earlier.get("status") == "open":
                rep.check(False, f"forecasts {earlier['id']} and {rid} are both live on "
                                 f"{key}. One live row per (forecaster, venue, market, "
                                 "horizon); a new one is refused, never superseded")
            else:
                closed = str(earlier.get("settled_ts") or "")
                rep.check(bool(closed) and closed <= str(row["ts"]),
                          f"forecast {rid} was written at {row['ts']} while {earlier['id']} "
                          f"on the same key was live until {closed or 'an unknown time'}. "
                          "A live forecast is never superseded")
        history.setdefault(key, []).append(row)

    by = {s: sum(1 for r in rows if r.get("status") == s) for s in forecast.STATUSES}
    rep.note(f"{len(rows)} forecast rows: {by['open']} open, {by['settled']} settled, "
             f"{by['void']} void")
    return rows


# --------------------------------------------------------------------------
# The experiment ledger
# --------------------------------------------------------------------------

def check_experiments(rep: Report, root: Path = ROOT) -> None:
    """The ledger's own rules, from the grader that reads it. A broken open
    row fails until the agent appends its `withdrawn` close row; the grader's
    baseline check is `core/experiments.py grade`'s, since it needs the
    journals and not just the ledger."""
    path = root / experiments.LEDGER_REL
    if not path.exists():
        rep.note(f"{experiments.LEDGER_REL} does not exist yet; no experiment has been opened")
        return
    try:
        failures, notes = experiments.ledger_problems(root, path)
    except experiments.ExperimentError as err:
        rep.check(False, f"{experiments.LEDGER_REL}: {err}")
        return
    for failure in failures:
        rep.check(False, f"{experiments.LEDGER_REL}: {failure}")
    rep.check(True, f"{experiments.LEDGER_REL} folds")
    for note in notes:
        rep.note(note)


# --------------------------------------------------------------------------
# The venue record and the code
# --------------------------------------------------------------------------

def check_venues(rep: Report, cfg: dict) -> None:
    path = ROOT / "journal" / "venues.jsonl"
    if not path.exists():
        rep.note("journal/venues.jsonl does not exist yet")
        return
    rows, bad_ids, markets = [], 0, 0
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as err:
            rep.check(False, f"venues.jsonl line {n} is not JSON: {err}")
            return
    for row in rows:
        if not rep.check(bool(row.get("address")) and bool(row.get("kind")),
                         f"a venue row has no address or kind: {str(row)[:80]}"):
            continue
        rep.check(row.get("confirmed_block") is not None,
                  f"{row['address']} has no confirming block")
        if row["kind"] != "morpho_market" or "market_id" not in row:
            continue
        markets += 1
        # The record stores the loan token by symbol, because every one of these
        # markets lends USDG; the id is built from the address the market was
        # created with.
        try:
            body = (abi.encode_address(cfg["quote_asset_address"])
                    + abi.encode_address(row["collateral_token"])
                    + abi.encode_address(row["oracle"])
                    + abi.encode_address(row["irm"])
                    + abi.encode_uint(abi.str_to_fixed(row["lltv"], 18)))
        except (KeyError, abi.AbiError):
            bad_ids += 1
            continue
        if "0x" + abi.keccak256(body).hex() != str(row["market_id"]).lower():
            bad_ids += 1
    rep.check(bad_ids == 0,
              f"{bad_ids} of {markets} Morpho market ids do not reproduce from their "
              "own stored parameters. The params used to read a rate would not be "
              "the market's")
    rep.note(f"{len(rows)} venue rows, {markets} Morpho market ids reproduce")


def check_python(rep: Report) -> None:
    files = sorted(list((ROOT / "core").rglob("*.py"))
                   + list((ROOT / "strategy").rglob("*.py")))
    with tempfile.TemporaryDirectory() as tmp:
        for path in files:
            try:
                py_compile.compile(str(path), cfile=str(Path(tmp) / "out.pyc"),
                                   doraise=True)
            except py_compile.PyCompileError as err:
                rep.check(False, f"{path.relative_to(ROOT)} does not compile: {err}")
    rep.check(True, f"{len(files)} Python files compile")

    for path in files:
        if path.parent.name != "core" or path.name == "__init__.py":
            continue
        head = path.read_text()[:1200]
        rep.check("PROTECTED ENGINE FILE" in head,
                  f"core/{path.name} does not say at the top that it is protected")


def check_abi(rep: Report) -> None:
    problems = abi.check_table()
    rep.check(not problems,
              "the ABI table does not recompute from its own signatures: "
              + "; ".join(problems[:3]))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run() -> Report:
    rpc.forbid_network("core/validate.py is offline by design; CI runs it with no "
                       "endpoint and a tripwire that needs the chain is not a tripwire")
    rep = Report()
    cfg = check_config(rep)
    if cfg:
        check_strategy(rep, cfg)
        check_ledger(rep, cfg)
        check_forecasts(rep)
        check_venues(rep, cfg)
    check_experiments(rep)
    check_abi(rep)
    check_python(rep)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rep = run()
    if args.json:
        print(json.dumps(rep.as_dict(), indent=1))
    else:
        for note in rep.notes:
            print(f"note: {note}")
        for failure in rep.failures:
            print(f"FAIL: {failure}")
        print(f"\n{rep.checks} checks, {len(rep.failures)} failures")
    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
