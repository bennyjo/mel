"""The replay gate: score a forecast rule on the settled forecast ledger.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

A forecast edit judged on live rows waits weeks for a verdict. This judges it
the same day: `strategy/forecast_rule.py`, as it stands in the working tree,
forecasts every settled row of `journal/forecasts.jsonl` again, and its error
is paired row by row against two rules:

  - the current rule, the same file at a git ref (`HEAD` by default). Before
    the file's first commit there is none, and the report says so.
  - the trailing-24h baseline: the row's recorded trailing 24-hour rate, else
    its trailing 7-day rate, else its advertised rate, because the 24-hour
    rate alone is null on a third of the rows.

The rule is a module-level function:

    def forecast(snapshot: dict) -> float | None:
        '''The gross annual rate as a fraction, or None for no forecast.'''

It is called once per row, oldest entry first, and the snapshot holds what the
row knew at entry and nothing else: `SNAPSHOT_FIELDS`. That is an allowlist,
so a field the ledger gains later stays out until someone adds it here. Python
cannot stop a rule from opening the ledger itself. So the replay is a filter,
not a score: live forecasts still go through `core/forecast.py record` before
any outcome exists, and a rule that cheats here shows up there.

The ledger is cut at the UTC day of its median row, and each half is judged
alone, so a rule fitted to one regime cannot average its way past the other.
The rule passes if, in both halves and against both rules, it does not lose.
It loses when the 95% cluster-robust interval of |rule error| - |other error|
lies above zero. It also fails, rather than passes, on a half with too few
rows to say (`core/score.py` sets the bar) and on any row where it abstains
and the other rule spoke, because skipping the hard rows is not forecasting.

Only lending rows are scored. A Uniswap v3 row's rate moves with a price, up
to thousands of percent, and one such row outweighs every lending row.

This writes nothing and reads no chain state: network use raises.

Commands:
    python3 core/replay.py               # the working tree against HEAD
    python3 core/replay.py --ref HEAD~1  # after the edit is committed

Exit status: 0 passed, 1 did not pass, 2 the rule could not be replayed.
"""

from __future__ import annotations

import argparse
import copy
import math
import statistics
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import forecast, rpc, score

RULE_PATH = ROOT / "strategy" / "forecast_rule.py"
KINDS = ("morpho_market", "erc4626_usdg_vault")
HALVES = ("older", "newer")
BASELINE_ORDER = ("trailing_24h", "trailing_7d", "advertised")
SNAPSHOT_FIELDS = ("venue", "market_id", "name", "kind", "horizon_min", "entry_ts",
                   "entry_block", "entry_depth_usdg", "advertised", "trailing_24h",
                   "trailing_7d", "entry_state")
PASSING = ("beats", "ties")

Rule = Callable[[dict], object]


class ReplayError(Exception):
    """The rule could not be loaded or run, or the ref does not exist."""


def snapshot(row: dict) -> dict:
    """What the row knew at entry. A copy, so a rule cannot change the row or
    leave a note on it for a later call."""
    predictions = forecast.predictions(row)
    return {"venue": row["venue"], "market_id": row.get("market_id"),
            "name": row.get("name"), "kind": row["kind"],
            "horizon_min": int(row["horizon_min"]), "entry_ts": int(row["entry_ts"]),
            "entry_block": row.get("entry_block"),
            "entry_depth_usdg": row.get("entry_depth_usdg"),
            "advertised": predictions["advertised"],
            "trailing_24h": predictions["trailing_24h"],
            "trailing_7d": predictions["trailing_7d"],
            "entry_state": copy.deepcopy(row.get("entry_state") or {})}


def load_rule(source: str, label: str) -> Rule:
    """The `forecast` function of a rule's source, in a module of its own."""
    module = types.ModuleType("forecast_rule")
    try:
        exec(compile(source, label, "exec"), module.__dict__)
    except Exception as err:
        raise ReplayError(f"{label} does not load: {err!r}") from err
    rule = getattr(module, "forecast", None)
    if not callable(rule):
        raise ReplayError(f"{label} has no forecast(snapshot) function")
    return rule


def source_at(ref: str) -> str | None:
    """The rule's source at a git ref, or None if the ref predates the file."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)

    if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode:
        raise ReplayError(f"no such git ref: {ref}")
    shown = git("show", f"{ref}:{RULE_PATH.relative_to(ROOT).as_posix()}")
    return shown.stdout if shown.returncode == 0 else None


def scored_rows(rows: list[dict]) -> list[dict]:
    """The settled lending rows, oldest entry first."""
    return sorted((r for r in rows if r.get("status") == "settled" and r.get("kind") in KINDS),
                  key=lambda r: (int(r["entry_ts"]), r["id"]))


def predict(rule: Rule, rows: list[dict], label: str = "the rule") -> dict[str, float | None]:
    """A rule's number for each row, by row id, called in the order given."""
    out: dict[str, float | None] = {}
    for row in rows:
        try:
            value = rule(snapshot(row))
        except Exception as err:
            raise ReplayError(f"{label} failed on {row['id']}: {err!r}") from err
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value)):
            raise ReplayError(f"{label} returned {value!r} on {row['id']}: "
                              "a forecast is a finite number or None")
        out[row["id"]] = None if value is None else float(value)
    return out


def baseline(rows: list[dict]) -> dict[str, float | None]:
    """The trailing-24h baseline's number for each row, by row id."""
    out: dict[str, float | None] = {}
    for row in rows:
        predictions = forecast.predictions(row)
        out[row["id"]] = next((predictions[name] for name in BASELINE_ORDER
                               if predictions[name] is not None), None)
    return out


def _day(row: dict) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(row["entry_ts"])))


def split(rows: list[dict]) -> dict[str, list[dict]]:
    """The two halves, cut at the UTC day of the median row so that no (kind,
    day) cluster straddles them. A ledger of one day has an empty older half,
    which fails rather than passes."""
    if not rows:
        return {name: [] for name in HALVES}
    cut = sorted(_day(r) for r in rows)[len(rows) // 2]
    return {"older": [r for r in rows if _day(r) < cut],
            "newer": [r for r in rows if _day(r) >= cut]}


def judge(stat: dict, abstained: int) -> str:
    """What one paired comparison says about the rule, in one word."""
    if abstained:
        return "abstains"
    if not stat["n"]:
        return "no-rows"
    if not score.enough(stat):
        return "too-few"
    if stat["lo"] > 0:
        return "loses"
    return "beats" if stat["hi"] < 0 else "ties"


def compare(rows: list[dict], mine: dict, theirs: dict) -> dict:
    """The rule against another on the rows where the other spoke. A row where
    the rule abstains is counted, not paired."""
    diffs, clusters, abstained = [], [], 0
    for row in rows:
        other = theirs[row["id"]]
        if other is None:
            continue
        if mine[row["id"]] is None:
            abstained += 1
            continue
        realized = float(row["realized_gross_apy"])
        diffs.append(abs(realized - mine[row["id"]]) - abs(realized - other))
        clusters.append(score.cluster_of(row))
    stat = score.clustered_mean(diffs, clusters)
    return {"diff": stat, "abstained": abstained, "verdict": judge(stat, abstained)}


def summary(rows: list[dict], numbers: dict) -> dict:
    """One rule's mean absolute error and bias, realized minus predicted, on
    the rows where it spoke."""
    errors = [float(r["realized_gross_apy"]) - numbers[r["id"]] for r in rows
              if numbers[r["id"]] is not None]
    return {"n": len(errors),
            "mae": statistics.fmean(abs(e) for e in errors) if errors else None,
            "bias": statistics.fmean(errors) if errors else None}


def replay(rows: list[dict], rule: Rule, current: Rule | None = None,
           ref: str = "HEAD") -> dict:
    """The whole verdict on a list of forecast-ledger rows."""
    scored = scored_rows(rows)
    mine = predict(rule, scored)
    others = {"t24h": baseline(scored)}
    if current is not None:
        others[ref] = predict(current, scored, f"the rule at {ref}")
    halves = split(scored)
    versus = {name: {half: compare(part, mine, theirs) for half, part in halves.items()}
              for name, theirs in others.items()}
    return {"n": len(scored), "halves": {h: len(p) for h, p in halves.items()},
            "newer_from": _day(halves["newer"][0]) if halves["newer"] else None,
            "summary": {name: {h: summary(p, numbers) for h, p in halves.items()}
                        for name, numbers in {"rule": mine, **others}.items()},
            "versus": versus,
            "first_rule": current is None,
            "passed": all(c["verdict"] in PASSING for by_half in versus.values()
                          for c in by_half.values())}


def _interval(stat: dict) -> str:
    if stat["mean"] is None:
        return "no rows"
    if stat["lo"] is None:
        return f"{score._pct(stat['mean'], 0)} [no interval]"
    return (f"{score._pct(stat['mean'], 0)} "
            f"[{score._pct(stat['lo'], 0)}, {score._pct(stat['hi'], 0)}]")


def replay_line(data: dict) -> str:
    """The one line a commit message quotes."""
    parts = [f"vs {name} " + ", ".join(f"{half} {_interval(c['diff'])} {c['verdict']}"
                                       for half, c in by_half.items())
             for name, by_half in data["versus"].items()]
    if data["first_rule"]:
        parts.append("no earlier rule")
    return f"replay {'PASS' if data['passed'] else 'FAIL'}, {data['n']} rows: " + "; ".join(parts)


def print_report(data: dict) -> None:
    print(f"Replay on {data['n']} settled lending rows: {data['halves']['older']} older, "
          f"{data['halves']['newer']} newer from {data['newer_from']}")
    print(f"\n{'predictor':12s} {'half':6s} {'n':>5s} {'MAE':>8s} {'bias':>8s}")
    for name, by_half in data["summary"].items():
        for half, s in by_half.items():
            print(f"{name[:12]:12s} {half:6s} {s['n']:>5d} {score._pct(s['mae'], sign=False)} "
                  f"{score._pct(s['bias'])}")
    print("\nmean of |rule error| - |other error|, negative is the rule closer")
    for name, by_half in data["versus"].items():
        for half, c in by_half.items():
            d = c["diff"]
            print(f"  vs {name[:12]:12s} {half:6s} n {d['n']:>4d} clus {d['clusters']:>3d} "
                  f"n_eff {d['n_eff']:>6.1f}  {_interval(d)}  {c['verdict']}"
                  + (f" on {c['abstained']} rows" if c["abstained"] else ""))
    print("\n" + replay_line(data))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="HEAD", help="git ref of the current rule (default HEAD)")
    args = ap.parse_args(argv)

    rpc.forbid_network("core/replay.py reads the forecast ledger and nothing else")
    name = RULE_PATH.relative_to(ROOT).as_posix()
    try:
        if not RULE_PATH.exists():
            raise ReplayError(f"{name} does not exist yet")
        rule = load_rule(RULE_PATH.read_text(), name)
        source = source_at(args.ref)
        current = None if source is None else load_rule(source, f"{name} at {args.ref}")
        data = replay(forecast.load(), rule, current, args.ref)
    except ReplayError as err:
        print(f"replay error: {err}", file=sys.stderr)
        return 2
    print_report(data)
    return 0 if data["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
