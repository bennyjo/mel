"""The experiment grader: one verdict line per self-improvement experiment.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

An experiment is the unit of self-improvement. The deep retro proposes one,
implements it in one commit under `strategy/`, pre-registers a metric from the
catalog below with a computed baseline, a window and two bars, and acts on the
verdict this file prints. The ledger is `strategy/experiments.jsonl`,
agent-owned and append-only (`.github/scripts/boundary.sh`), event-sourced so
append-only is enough. One row per event, folded by `id`:

    {"event": "open", "id": "E-2026-11-17-a", "ts": "2026-11-17T08:50:00Z",
     "hypothesis": "...", "lever": ["strategy/forecast_rule.py"],
     "commit": "<sha of the experiment commit>",
     "metric": "agent_vs_best_mae:morpho_market", "baseline": -0.0012,
     "window": {"rows": 20}, "keep_if": "< -0.002", "rollback_if": "> 0",
     "prior": [], "blocked_by_core": null}
    {"event": "verdict", "id": "E-2026-11-17-a", "ts": "...", "verdict": "KEEP",
     "value": -0.004, "n": 20, "source": "core/experiments.py grade"}
    {"event": "close", "id": "E-2026-11-17-a", "ts": "...", "outcome": "kept",
     "lesson": "one line that goes in the playbook"}

An open row with `blocked_by_core` set and no `commit` records a lever only
the operator can pull. It is not an experiment: it is never graded and does
not count against the open cap. It shows the loop is blocked, not idle.

What the grader enforces, so that a verdict is never a hand tally:

  - `commit` is an ancestor of HEAD and touches one or two files, all under
    `strategy/` and none of them the ledger, so `git revert` undoes exactly
    the experiment;
  - every `lever` is a path under `strategy/` (an optional `#section`
    follows it), and no two open experiments share one;
  - a lever rolled back in the last 30 days needs `prior` to cite the rolled
    back id;
  - at most `MAX_OPEN` experiments are open;
  - `metric` and every metric a bar names are in the catalog, which is this
    file: adding a metric is an operator change, because it defines success;
  - `baseline` is what `propose` computes for the same window ending at the
    commit, so a baseline is never typed by hand;
  - the commit loosens no cap in `strategy/risk.json`. Loosening a gate is an
    operator act; an experiment may only tighten one or run a replay.

The forward window starts at the commit's own timestamp, the same sealed
cutoff `core/replay.py` uses between its halves: only rows that began after
the change count for it. A window is `{"days": D}` (rows that began and
ended inside D days from the commit, or the D days before it for the
baseline) or `{"rows": N}` (the first N rows that began after the commit and
have ended, or the last N that ended before it).

Verdicts:

  COLLECTING    the window has not run out, and no early rollback applies
  ROLL BACK     `rollback_if` holds once half the window has accrued, or at
                the end of the window `keep_if` does not and `rollback_if` does
  KEEP          at the end of the window `keep_if` holds and `rollback_if`
                does not
  DOUBLE DOWN   KEEP with the effect at least twice the bar (value minus
                baseline against threshold minus baseline), or the metric's
                95% interval entirely past the threshold
  INCONCLUSIVE  the window ran out and neither bar holds, or the metric had
                too few rows to speak
  REFUSED       the open row breaks a rule above; the verdict is not graded

This writes nothing and reads no chain state: network use raises.

Commands:
    python3 core/experiments.py propose --metric agent_rows_per_day --window days=7
    python3 core/experiments.py propose --metric idle_share --window days=7 --commit <sha>
    python3 core/experiments.py grade [--json]
    python3 core/experiments.py status [--lever strategy/forecast_rule.py] [--json]

Exit status: 0 fine, 1 a REFUSED experiment (grade) or a bad request
(propose), 2 the ledger does not parse.
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import forecast, ledger, replay, rpc, score

LEDGER_REL = "strategy/experiments.jsonl"
DAY_S = 86_400
MAX_OPEN = 3
MAX_FILES = 2
FAILED_LOOKBACK_S = 30 * DAY_S
EVENTS = ("open", "verdict", "close")
VERDICTS = ("COLLECTING", "KEEP", "ROLL BACK", "DOUBLE DOWN", "INCONCLUSIVE")
OUTCOMES = ("kept", "rolled_back", "inconclusive", "withdrawn")
BLOCKED_OUTCOMES = ("unblocked", "withdrawn")
ID_RE = re.compile(r"^E-\d{4}-\d{2}-\d{2}-[a-z]$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LENDING = replay.KINDS
BASELINE_TOLERANCE = 1e-6

# The caps in strategy/risk.json an experiment commit may only move the safe
# way: core/validate.py's tighten-only set, plus the agent's own two caps that
# the operator's config has no twin for.
RISK_TIGHTEN_ONLY = {"max_round_trip_bps": "<=", "max_market_share_usdg": "<="}


class ExperimentError(Exception):
    """The ledger does not parse, or a request cannot be answered."""


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def resolve(root: Path, ref: str) -> str | None:
    """The full sha of a commit, or None."""
    out = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else None


def is_ancestor(root: Path, sha: str, of: str = "HEAD") -> bool:
    return _git(root, "merge-base", "--is-ancestor", sha, of).returncode == 0


def commit_ts(root: Path, sha: str) -> int:
    return int(_git(root, "log", "-1", "--format=%ct", sha).stdout.strip())


def commit_files(root: Path, sha: str) -> list[str]:
    out = _git(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha)
    return [line for line in out.stdout.splitlines() if line]


def show(root: Path, ref: str, path: str) -> str | None:
    out = _git(root, "show", f"{ref}:{path}")
    return out.stdout if out.returncode == 0 else None


def risk_loosenings(root: Path, sha: str) -> list[str]:
    """Each cap in strategy/risk.json the commit moved the risky way, or
    dropped, which hands it back to the looser operator cap."""
    from core.validate import TIGHTEN_ONLY  # validate imports this module

    before, after = (show(root, ref, "strategy/risk.json") for ref in (f"{sha}^", sha))
    if before is None or after is None or before == after:
        return []
    try:
        old, new = json.loads(before), json.loads(after)
    except json.JSONDecodeError:
        return []
    out = []
    for key, op in {**TIGHTEN_ONLY, **RISK_TIGHTEN_ONLY}.items():
        if key not in old:
            continue
        if key not in new:
            out.append(f"{key} removed (was {old[key]})")
            continue
        try:
            a, b = float(old[key]), float(new[key])
        except (TypeError, ValueError):
            continue
        if (b > a) if op == "<=" else (b < a):
            out.append(f"{key} {a:g} -> {b:g}")
    return out


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------

def parse_ts(value: object) -> int | None:
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        return None
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as err:
            raise ExperimentError(f"{path.name} line {n} is not JSON: {err}") from err
        if not isinstance(row, dict):
            raise ExperimentError(f"{path.name} line {n} is not a JSON object")
        rows.append(row)
    return rows


def lever_path(lever: str) -> str:
    return lever.split("#", 1)[0]


def levers_overlap(a: str, b: str) -> bool:
    """Same file, and either names the whole file or both name one section."""
    if lever_path(a) != lever_path(b):
        return False
    return "#" not in a or "#" not in b or a == b


@dataclass
class Experiment:
    id: str
    open: dict
    verdicts: list
    close: dict | None
    problems: list

    @property
    def blocked(self) -> bool:
        return bool(self.open.get("blocked_by_core"))

    @property
    def state(self) -> str:
        if self.close is not None:
            return "closed"
        return "blocked" if self.blocked else "open"


def _check_open(row: dict, root: Path, active: list[Experiment],
                closed: list[Experiment]) -> list[str]:
    """What is wrong with an open row, judged against the experiments open
    and closed at the moment it was appended."""
    problems = []
    if not isinstance(row.get("hypothesis"), str) or not row["hypothesis"].strip():
        problems.append("has no hypothesis")
    levers = row.get("lever")
    if not (isinstance(levers, list) and levers
            and all(isinstance(x, str) and x for x in levers)):
        problems.append("lever must be a non-empty list of paths")
        levers = []
    prior = row.get("prior", [])
    if not (isinstance(prior, list) and all(isinstance(x, str) for x in prior)):
        problems.append("prior must be a list of ids (or [\"none\"])")
        prior = []
    blocked = row.get("blocked_by_core")
    if blocked is not None and not (isinstance(blocked, str) and blocked.strip()):
        problems.append("blocked_by_core must be null or say what is blocked")

    if blocked:
        if row.get("commit") is not None:
            problems.append("a blocked_by_core row has no commit: nothing was changed")
        return problems

    for lever in levers:
        path = lever_path(lever)
        if not path.startswith("strategy/") or path == LEDGER_REL or ".." in path:
            problems.append(f"lever {lever} is not an agent-owned file under strategy/. "
                            "A protected lever is a blocked_by_core row, not an experiment")
    live = [e for e in active if not e.blocked]
    if len(live) >= MAX_OPEN:
        problems.append(f"{len(live)} experiments are already open; the cap is {MAX_OPEN}")
    for other in live:
        for mine in levers:
            if any(levers_overlap(mine, theirs) for theirs in other.open.get("lever") or []):
                problems.append(f"lever {mine} is already held by open experiment {other.id}")
    opened = parse_ts(row.get("ts")) or 0
    for done in closed:
        when = parse_ts(done.close.get("ts")) or 0
        if (done.close.get("outcome") != "rolled_back" or opened - when > FAILED_LOOKBACK_S
                or done.id in prior):
            continue
        if any(levers_overlap(mine, theirs) for mine in levers
               for theirs in done.open.get("lever") or []):
            problems.append(f"lever was rolled back in {done.id} within 30 days; prior "
                            "must cite it and the hypothesis say what is different")

    try:
        spec = parse_metric(row.get("metric"))
        window = parse_window(row.get("window"))
        if window[0] not in spec.metric.windows:
            problems.append(f"metric {spec.name} takes a window of "
                            f"{' or '.join(spec.metric.windows)}, not {window[0]}")
        for field in ("keep_if", "rollback_if"):
            for clause in _flat(parse_bar(row.get(field), spec.name)):
                if window[0] not in clause.spec.metric.windows:
                    problems.append(f"{field} names {clause.spec.name}, which cannot be "
                                    f"read over a {window[0]} window")
    except ExperimentError as err:
        problems.append(str(err))
    if row.get("baseline") is not None and not _is_number(row.get("baseline")):
        problems.append("baseline must be the number propose printed, or null")
    problems.extend(_check_commit(row.get("commit"), root))
    return problems


def _check_commit(commit: object, root: Path) -> list[str]:
    if not isinstance(commit, str) or not commit:
        return ["has no commit. An experiment is one commit under strategy/"]
    sha = resolve(root, commit)
    if sha is None:
        return [f"commit {commit} does not resolve in this clone"]
    if not is_ancestor(root, sha):
        return [f"commit {commit} is not an ancestor of HEAD"]
    problems = []
    files = commit_files(root, sha)
    if not files or len(files) > MAX_FILES:
        problems.append(f"commit {commit} touches {len(files)} files; an experiment "
                        f"touches one or {MAX_FILES}")
    outside = [f for f in files if not f.startswith("strategy/") or f == LEDGER_REL]
    if outside:
        problems.append(f"commit {commit} touches {', '.join(outside)}: an experiment "
                        "commit changes strategy/ files only and never the ledger, "
                        "so a revert undoes exactly the change")
    loose = risk_loosenings(root, sha)
    if loose:
        problems.append(f"commit {commit} loosens strategy/risk.json ({'; '.join(loose)}). "
                        "Loosening a gate is an operator act")
    return problems


def _is_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def fold(rows: list[dict], root: Path = ROOT) -> tuple[dict[str, Experiment], list[str]]:
    """Every experiment by id, in file order, and the problems of rows that
    attach to no experiment."""
    experiments: dict[str, Experiment] = {}
    loose: list[str] = []
    last = 0
    for n, row in enumerate(rows, 1):
        rid, event = row.get("id"), row.get("event")
        where = f"line {n} ({rid or 'no id'})"
        ts = parse_ts(row.get("ts"))
        if ts is None:
            loose.append(f"{where}: ts is not YYYY-MM-DDTHH:MM:SSZ")
        elif ts < last:
            loose.append(f"{where}: ts is earlier than the row before; the ledger is "
                         "appended in time order")
        last = max(last, ts or 0)
        if event not in EVENTS:
            loose.append(f"{where}: event must be one of {', '.join(EVENTS)}")
            continue
        if not isinstance(rid, str) or not ID_RE.fullmatch(rid):
            loose.append(f"{where}: id must look like E-YYYY-MM-DD-a")
            continue
        if event == "open":
            if rid in experiments:
                loose.append(f"{where}: {rid} is opened twice")
                continue
            active = [e for e in experiments.values() if e.close is None]
            closed = [e for e in experiments.values() if e.close is not None]
            experiments[rid] = Experiment(rid, row, [], None,
                                          _check_open(row, root, active, closed))
            continue
        exp = experiments.get(rid)
        if exp is None:
            loose.append(f"{where}: {event} for an experiment that was never opened")
            continue
        if exp.close is not None:
            loose.append(f"{where}: {event} after {rid} was closed")
            continue
        if event == "verdict":
            if exp.blocked:
                loose.append(f"{where}: a blocked_by_core row is never graded")
            elif row.get("verdict") not in VERDICTS:
                loose.append(f"{where}: verdict must be one of {', '.join(VERDICTS)}")
            elif row.get("source") != "core/experiments.py grade":
                loose.append(f"{where}: a verdict comes from core/experiments.py grade, "
                             "never from a hand tally")
            exp.verdicts.append(row)
            continue
        outcomes = BLOCKED_OUTCOMES if exp.blocked else OUTCOMES
        if row.get("outcome") not in outcomes:
            loose.append(f"{where}: outcome must be one of {', '.join(outcomes)}")
        if not isinstance(row.get("lesson"), str) or not row["lesson"].strip():
            loose.append(f"{where}: a close row carries a one-line lesson")
        exp.close = row
    return experiments, loose


def ledger_problems(root: Path = ROOT, path: Path | None = None) -> tuple[list[str], list[str]]:
    """The failures and notes `core/validate.py` reports. A broken open row
    fails only while its experiment is open: appending a `withdrawn` close is
    how the agent clears it."""
    rows = load(path or root / LEDGER_REL)
    experiments, loose = fold(rows, root)
    failures = list(loose)
    notes = []
    for exp in experiments.values():
        for problem in exp.problems:
            if exp.close is None:
                failures.append(f"experiment {exp.id} {problem}")
            else:
                notes.append(f"closed experiment {exp.id} {problem}")
    live = [e for e in experiments.values() if e.state == "open"]
    if len(live) > MAX_OPEN:
        failures.append(f"{len(live)} experiments are open; the cap is {MAX_OPEN}")
    by = {s: sum(1 for e in experiments.values() if e.state == s)
          for s in ("open", "blocked", "closed")}
    notes.append(f"{len(experiments)} experiments: {by['open']} open, "
                 f"{by['blocked']} blocked by core, {by['closed']} closed")
    return failures, notes


# --------------------------------------------------------------------------
# Windows, bars and the metric catalog
# --------------------------------------------------------------------------

def parse_window(value: object) -> tuple[str, float]:
    if isinstance(value, str):
        key, _, num = value.partition("=")
        try:
            value = {key: float(num) if key == "days" else int(num)}
        except ValueError:
            raise ExperimentError(f"window {value!r} is not days=<n> or rows=<n>")
    if not isinstance(value, dict) or len(value) != 1:
        raise ExperimentError("window must be {\"days\": n} or {\"rows\": n}")
    (key, num), = value.items()
    if key not in ("days", "rows") or not _is_number(num) or num <= 0:
        raise ExperimentError("window must be {\"days\": n} or {\"rows\": n}, n above zero")
    if key == "rows" and int(num) != num:
        raise ExperimentError("a rows window counts whole rows")
    return key, float(num)


@dataclass
class Result:
    value: float | None
    n: int
    lo: float | None = None
    hi: float | None = None
    detail: str = ""


@dataclass
class Item:
    start: int     # when the row began: entry, or the record time
    end: int       # when it ended: settlement, or the record time again
    row: dict


@dataclass
class Context:
    root: Path
    commit: str | None
    since: int     # the window's first second
    until: int     # one past its last second
    days: float    # the window's span in days
    schedule_ref: str = "HEAD"  # where strategy/schedule.json is read


@dataclass
class Metric:
    doc: str
    windows: tuple
    items: Callable[["Journals", str | None], list[Item]]
    value: Callable[[list[Item], "Journals", str | None, Context], Result]
    param: str | None = None


class Journals:
    """The journals, read once per process."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, list[dict]] = {}

    def _get(self, name: str, loader: Callable[[Path], list[dict]]) -> list[dict]:
        if name not in self._cache:
            path = self.root / "journal" / name
            self._cache[name] = loader(path) if path.exists() else []
        return self._cache[name]

    @property
    def forecasts(self) -> list[dict]:
        return self._get("forecasts.jsonl", forecast.load)

    @property
    def allocations(self) -> list[dict]:
        return self._get("allocations.jsonl", ledger.load)

    @property
    def usage(self) -> list[dict]:
        return self._get("rpc-usage.jsonl", load)


def _ts(row: dict, field: str) -> int | None:
    value = row.get(field)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return parse_ts(value)


def _settled_forecasts(j: Journals, kinds: tuple, forecaster: str | None) -> list[Item]:
    out = []
    for r in j.forecasts:
        if r.get("status") != "settled" or r.get("kind") not in kinds:
            continue
        if forecaster and r.get("forecaster") != forecaster:
            continue
        start, end = _ts(r, "entry_ts"), _ts(r, "settled_ts")
        if start is not None and end is not None:
            out.append(Item(start, end, r))
    return out


def _kinds(param: str | None) -> tuple:
    if param in (None, "", "lending"):
        return LENDING
    if param not in LENDING:
        raise ExperimentError(f"kind {param} is not one of lending, {', '.join(LENDING)}")
    return (param,)


# agent_rows_per_day ---------------------------------------------------------

def _agent_rows_items(j: Journals, param: str | None) -> list[Item]:
    out = []
    for r in j.forecasts:
        ts = _ts(r, "ts")
        if r.get("forecaster") == "agent" and ts is not None:
            out.append(Item(ts, ts, r))
    return out


def _per_day(items: list[Item], j: Journals, param: str | None, ctx: Context) -> Result:
    if ctx.days <= 0:
        return Result(None, len(items))
    return Result(len(items) / ctx.days, len(items),
                  detail=f"{len(items)} rows over {ctx.days:.2f} days")


# graded_rows_per_day ----------------------------------------------------------

def _graded_rows_items(j: Journals, param: str | None) -> list[Item]:
    """Agent rows that settled, placed at their settle time: a void is the
    gap between this and agent_rows_per_day, and it is known only then."""
    out = []
    for r in j.forecasts:
        ts = _ts(r, "settled_ts")
        if r.get("forecaster") == "agent" and r.get("status") == "settled" and ts is not None:
            out.append(Item(ts, ts, r))
    return out


# agent_vs_best_mae:<kind> -----------------------------------------------------

def _agent_items(j: Journals, param: str | None) -> list[Item]:
    return _settled_forecasts(j, _kinds(param), "agent")


def _vs_best(items: list[Item], j: Journals, param: str | None, ctx: Context) -> Result:
    """Mean of |agent error| - |best baseline error|, as core/score.py pairs
    them; negative is the agent closer. The best baseline is ranked on the
    rows settled before the window closed, so a baseline recomputes the same."""
    pool = [r for r in j.forecasts if r.get("status") == "settled"
            and (_ts(r, "settled_ts") or 0) < ctx.until]
    ranks = score.best_baselines(pool)
    best = score.versus([i.row for i in items], ranks)["best"]["diff"]
    return Result(best["mean"], best["n"], best["lo"], best["hi"],
                  detail=f"{best['clusters']} clusters, n_eff {best['n_eff']:.1f}")


# abs_net_delta ----------------------------------------------------------------

def _deployed_items(j: Journals, param: str | None) -> list[Item]:
    out = []
    for r in j.allocations:
        if r.get("status") not in ledger.SCORED_STATUSES or r.get("kind") == "idle":
            continue
        start, end = _ts(r, "entry_ts"), _ts(r, "settled_ts")
        if start is not None and end is not None:
            out.append(Item(start, end, r))
    return out


def _abs_net_delta(items: list[Item], j: Journals, param: str | None, ctx: Context) -> Result:
    values = [abs(float(i.row["realized_net_apy"]) - float(i.row["forecast_net_apy"]))
              for i in items]
    stat = score.clustered_mean(values, [score.cluster_of(i.row) for i in items])
    return Result(stat["mean"], stat["n"], stat["lo"], stat["hi"])


# replay_pass:<rule file> -------------------------------------------------------

def _lending_items(j: Journals, param: str | None) -> list[Item]:
    return _settled_forecasts(j, LENDING, None)


def _replay_pass(items: list[Item], j: Journals, param: str | None, ctx: Context) -> Result:
    """core/replay.py's verdict on the rows in the window: the rule as the
    experiment commit left it against the rule before it, and against the
    trailing-24h baseline. 1 is PASS, 0 is FAIL, and too few rows to say is
    no value at all, so a thin window cannot fail an experiment early."""
    if not param or not param.startswith("strategy/") or not param.endswith(".py"):
        raise ExperimentError("replay_pass names a rule file under strategy/")
    if not ctx.commit:
        raise ExperimentError("replay_pass compares the rule at a commit with the "
                              "rule before it: pass --commit")
    source = show(ctx.root, ctx.commit, param)
    if source is None:
        raise ExperimentError(f"{param} does not exist at {ctx.commit[:12]}")
    before = show(ctx.root, f"{ctx.commit}^", param)
    try:
        rule = replay.load_rule(source, f"{param} at {ctx.commit[:12]}")
        current = None if before is None else replay.load_rule(before, f"{param} before")
        data = replay.replay([i.row for i in items], rule, current, "parent")
    except replay.ReplayError as err:
        return Result(0.0, len(items), detail=str(err))
    verdicts = [c["verdict"] for by in data["versus"].values() for c in by.values()]
    line = replay.replay_line(data)
    if any(v in ("too-few", "no-rows") for v in verdicts):
        return Result(None, data["n"], detail=line)
    return Result(1.0 if data["passed"] else 0.0, data["n"], detail=line)


# idle_share ---------------------------------------------------------------------

def _opened_items(j: Journals, param: str | None) -> list[Item]:
    out = []
    for r in j.allocations:
        ts = _ts(r, "ts")
        if ts is not None and r.get("status") != "void":
            out.append(Item(ts, ts, r))
    return out


def _idle_share(items: list[Item], j: Journals, param: str | None, ctx: Context) -> Result:
    total = sum(float(i.row["size_usdg"]) for i in items)
    idle = sum(float(i.row["size_usdg"]) for i in items if i.row.get("kind") == "idle")
    return Result(idle / total if total else None, len(items),
                  detail=f"{idle:g} of {total:g} USDG opened idle")


# full_cycle_seconds -------------------------------------------------------------

def _usage_items(j: Journals, param: str | None) -> list[Item]:
    out = []
    for r in j.usage:
        ts = _ts(r, "ts")
        if ts is not None and _is_number(r.get("wall_seconds")):
            out.append(Item(ts, ts, r))
    return out


def _full_hours(root: Path, ref: str) -> set[int]:
    text = show(root, ref, "strategy/schedule.json")
    try:
        hours = json.loads(text or "{}").get("full_cycle_hours_utc") or []
        return {int(h) for h in hours}
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return set()


def _cycle_seconds(items: list[Item], j: Journals, param: str | None, ctx: Context) -> Result:
    """Chain-reading seconds per full cycle: the wall seconds of every core
    run in a full-cycle UTC hour, summed per hour and averaged over those
    hours, from journal/rpc-usage.jsonl. The full hours are
    `strategy/schedule.json`'s as the window saw it: before the experiment
    commit for the baseline, at it after. The journals record no cycle's
    whole duration, so this is the closest measured number."""
    full = _full_hours(ctx.root, ctx.schedule_ref)
    hours: dict[int, float] = {}
    for i in items:
        hour = i.start // 3600
        if hour % 24 in full:
            hours[hour] = hours.get(hour, 0.0) + float(i.row["wall_seconds"])
    if not hours:
        return Result(None, 0, detail="no core run in a full-cycle hour")
    top = max(hours, key=hours.get)
    return Result(statistics.fmean(hours.values()), len(hours),
                  detail=f"{len(hours)} full cycles, slowest {hours[top]:.0f} s at "
                         f"{iso(top * 3600)[:13]}Z")


CATALOG: dict[str, Metric] = {
    "agent_rows_per_day": Metric(
        "agent forecast rows recorded per day", ("days",), _agent_rows_items, _per_day),
    "graded_rows_per_day": Metric(
        "agent forecast rows settled (not void) per day, by settle time", ("days",),
        _graded_rows_items, _per_day),
    "agent_vs_best_mae": Metric(
        "mean |agent error| - |best baseline error| on settled agent rows, "
        "negative is the agent closer", ("days", "rows"), _agent_items, _vs_best,
        param="<kind>"),
    "abs_net_delta": Metric(
        "mean |realized - forecast net APY| of settled deployed allocations",
        ("days", "rows"), _deployed_items, _abs_net_delta),
    "replay_pass": Metric(
        "core/replay.py on the window's settled lending rows, 1 PASS 0 FAIL",
        ("days", "rows"), _lending_items, _replay_pass, param="<rule file>"),
    "idle_share": Metric(
        "share of the USDG opened that was opened idle", ("days",), _opened_items,
        _idle_share),
    "full_cycle_seconds": Metric(
        "mean chain-reading seconds per full cycle", ("days",), _usage_items,
        _cycle_seconds),
}


@dataclass
class MetricSpec:
    name: str
    metric: Metric
    param: str | None


def parse_metric(value: object) -> MetricSpec:
    if not isinstance(value, str) or not value:
        raise ExperimentError("has no metric")
    base, _, param = value.partition(":")
    metric = CATALOG.get(base)
    if metric is None:
        raise ExperimentError(f"metric {base} is not in the catalog ({', '.join(CATALOG)}). "
                              "Adding one is an operator change")
    if bool(param) != bool(metric.param) and not (base == "agent_vs_best_mae" and not param):
        raise ExperimentError(f"metric {base} takes "
                              + (f"a parameter, {base}:{metric.param}" if metric.param
                                 else "no parameter"))
    if base == "agent_vs_best_mae":
        _kinds(param or None)
    if base == "replay_pass" and not (param.startswith("strategy/") and param.endswith(".py")):
        raise ExperimentError("replay_pass names a rule file under strategy/")
    return MetricSpec(value, metric, param or None)


@dataclass
class Clause:
    spec: MetricSpec
    op: str
    threshold: float

    def holds(self, value: float | None) -> bool:
        if value is None:
            return False
        return {"<": value < self.threshold, "<=": value <= self.threshold,
                ">": value > self.threshold, ">=": value >= self.threshold}[self.op]


CLAUSE_RE = re.compile(r"^(?:(?P<metric>[a-z_0-9]+(?::\S+)?)\s+)?"
                       r"(?P<op><=|>=|<|>)\s*(?P<num>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)$")


def parse_bar(text: object, own: str) -> list[list[Clause]]:
    """A bar is clauses joined by `or`, each a run of clauses joined by `and`.
    A clause is `[metric] <op> <number>`; with no metric it reads the
    experiment's own. `>= 30 and full_cycle_seconds < 600`."""
    if not isinstance(text, str) or not text.strip():
        raise ExperimentError("keep_if and rollback_if are both required")
    out = []
    for part in re.split(r"\s+or\s+", text.strip()):
        conj = []
        for raw in re.split(r"\s+and\s+", part.strip()):
            m = CLAUSE_RE.fullmatch(raw.strip())
            if not m:
                raise ExperimentError(f"bar clause {raw!r} is not '[metric] <op> <number>'")
            conj.append(Clause(parse_metric(m["metric"] or own), m["op"], float(m["num"])))
        out.append(conj)
    return out


def _flat(bar: list[list[Clause]]) -> list[Clause]:
    return [c for conj in bar for c in conj]


def bar_holds(bar: list[list[Clause]], values: dict[str, Result]) -> bool:
    return any(all(c.holds(values[c.spec.name].value) for c in conj) for conj in bar)


# --------------------------------------------------------------------------
# Measuring a window
# --------------------------------------------------------------------------

def measure(spec: MetricSpec, window: tuple[str, float], anchor: int, side: str,
            journals: Journals, now: float, commit: str | None = None) -> tuple[Result, float]:
    """A metric over the window before or after `anchor`, and how much of the
    window has accrued, as a fraction (always 1 before the anchor)."""
    kind, size = window
    items = spec.metric.items(journals, spec.param)
    if kind == "days":
        span = size * DAY_S
        if side == "before":
            since, until = anchor - span, anchor
        else:
            since, until = anchor, int(min(now, anchor + span))
        chosen = [i for i in items if i.start >= since and i.end < until]
        days = max(until - since, 0) / DAY_S
        accrued = 1.0 if side == "before" else min(days / size, 1.0)
    else:
        need = int(size)
        if side == "before":
            pool = sorted((i for i in items if i.end < anchor), key=lambda i: (i.end, i.start))
            chosen = pool[-need:]
            since = chosen[0].start if chosen else anchor
            until = anchor
        else:
            pool = sorted((i for i in items if i.start >= anchor and i.end < now),
                          key=lambda i: (i.end, i.start))
            chosen = pool[:need]
            since, until = anchor, int(now)
        days = max(until - since, 0) / DAY_S
        accrued = 1.0 if side == "before" else len(chosen) / need
    ref = (f"{commit}^" if side == "before" else commit) if commit else "HEAD"
    ctx = Context(journals.root, commit, since, until, days, ref)
    return spec.metric.value(chosen, journals, spec.param, ctx), accrued


def progress(window: tuple[str, float], accrued: float) -> str:
    kind, size = window
    if kind == "days":
        return f"{accrued * size:.1f}/{size:g} days"
    return f"{round(accrued * size)}/{int(size)} rows"


def judge(keep: list[list[Clause]], rollback: list[list[Clause]], own: str,
          values: dict[str, Result], accrued: float, baseline: object) -> str:
    keep_hit, rollback_hit = bar_holds(keep, values), bar_holds(rollback, values)
    if accrued < 1.0:
        return "ROLL BACK" if accrued >= 0.5 and rollback_hit else "COLLECTING"
    if rollback_hit:
        return "ROLL BACK"
    if not keep_hit:
        return "INCONCLUSIVE"
    result = values[own]
    for clause in (c for c in _flat(keep) if c.spec.name == own):
        if result.lo is not None and result.hi is not None and (
                clause.holds(result.hi) if clause.op in ("<", "<=") else clause.holds(result.lo)):
            return "DOUBLE DOWN"
        if _is_number(baseline) and result.value is not None:
            bar = clause.threshold - float(baseline)
            if bar and (result.value - float(baseline)) / bar >= 2.0:
                return "DOUBLE DOWN"
    return "KEEP"


def grade_one(exp: Experiment, journals: Journals, now: float) -> dict:
    row = exp.open
    out = {"id": exp.id, "metric": row.get("metric"), "lever": row.get("lever"),
           "keep_if": row.get("keep_if"), "rollback_if": row.get("rollback_if"),
           "baseline": row.get("baseline")}
    if exp.problems:
        return {**out, "verdict": "REFUSED", "reasons": exp.problems}
    root = journals.root
    sha = resolve(root, row["commit"])
    anchor = commit_ts(root, sha)
    spec = parse_metric(row["metric"])
    window = parse_window(row["window"])
    keep = parse_bar(row["keep_if"], spec.name)
    rollback = parse_bar(row["rollback_if"], spec.name)
    try:
        want, _ = measure(spec, window, anchor, "before", journals, now, sha)
        expected = want.value
        given = row.get("baseline")
        same = ((expected is None and given is None)
                or (expected is not None and _is_number(given)
                    and abs(expected - given) <= BASELINE_TOLERANCE * max(1.0, abs(expected))))
        if not same:
            return {**out, "verdict": "REFUSED",
                    "reasons": [f"baseline {given} is not the {expected} that propose "
                                "computes for this window ending at the commit"]}
        values: dict[str, Result] = {}
        accrued = 0.0
        for clause in [Clause(spec, ">", 0.0)] + _flat(keep) + _flat(rollback):
            if clause.spec.name in values:
                continue
            result, acc = measure(clause.spec, window, anchor, "after", journals, now, sha)
            values[clause.spec.name] = result
            if clause.spec.name == spec.name:
                accrued = acc
    except ExperimentError as err:
        return {**out, "verdict": "REFUSED", "reasons": [str(err)]}
    verdict = judge(keep, rollback, spec.name, values, accrued, row.get("baseline"))
    own = values[spec.name]
    return {**out, "verdict": verdict, "value": own.value, "n": own.n,
            "lo": own.lo, "hi": own.hi, "progress": progress(window, accrued),
            "detail": own.detail,
            "others": {k: v.value for k, v in values.items() if k != spec.name},
            "row": {"event": "verdict", "id": exp.id, "ts": iso(now), "verdict": verdict,
                    "value": own.value, "n": own.n, "source": "core/experiments.py grade"}}


def grade(root: Path = ROOT, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    experiments, _ = fold(load(root / LEDGER_REL), root)
    journals = Journals(root)
    out = []
    for exp in experiments.values():
        if exp.state == "closed":
            continue
        if exp.state == "blocked":
            out.append({"id": exp.id, "verdict": "BLOCKED", "lever": exp.open.get("lever"),
                        "blocked_by_core": exp.open.get("blocked_by_core"),
                        "since": exp.open.get("ts")})
            continue
        out.append(grade_one(exp, journals, now))
    return out


def propose(metric: str, window: str, root: Path = ROOT, commit: str | None = None,
            now: float | None = None) -> dict:
    """The baseline an open row must carry: the metric over the window that
    ends at the commit, or now when no commit is given yet."""
    now = time.time() if now is None else now
    spec = parse_metric(metric)
    win = parse_window(window)
    if win[0] not in spec.metric.windows:
        raise ExperimentError(f"metric {spec.name} takes a window of "
                              f"{' or '.join(spec.metric.windows)}")
    sha = None
    anchor = int(now)
    if commit:
        sha = resolve(root, commit)
        if sha is None:
            raise ExperimentError(f"commit {commit} does not resolve")
        anchor = commit_ts(root, sha)
    result, _ = measure(spec, win, anchor, "before", Journals(root), now, sha)
    return {"metric": spec.name, "window": {win[0]: int(win[1]) if win[0] == "rows" else win[1]},
            "commit": sha, "ends": iso(anchor), "baseline": result.value, "n": result.n,
            "lo": result.lo, "hi": result.hi, "detail": result.detail}


def status(root: Path = ROOT, lever: str | None = None) -> dict:
    experiments, loose = fold(load(root / LEDGER_REL), root)
    picked = [e for e in experiments.values()
              if lever is None or any(lever_path(x) == lever_path(lever)
                                      for x in e.open.get("lever") or [])]
    items = []
    for e in picked:
        last = e.verdicts[-1] if e.verdicts else {}
        items.append({"id": e.id, "state": e.state, "opened": e.open.get("ts"),
                      "hypothesis": e.open.get("hypothesis"), "lever": e.open.get("lever"),
                      "metric": e.open.get("metric"), "window": e.open.get("window"),
                      "commit": e.open.get("commit"),
                      "blocked_by_core": e.open.get("blocked_by_core"),
                      "last_verdict": last.get("verdict"),
                      "outcome": (e.close or {}).get("outcome"),
                      "lesson": (e.close or {}).get("lesson"),
                      "problems": e.problems})
    by = {s: sum(1 for e in experiments.values() if e.state == s)
          for s in ("open", "blocked", "closed")}
    return {"ledger": LEDGER_REL, "counts": by, "max_open": MAX_OPEN,
            "experiments": items, "ledger_problems": loose}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _num(value: object) -> str:
    return "-" if value is None else f"{value:.6g}"


def grade_line(r: dict) -> str:
    if r["verdict"] == "BLOCKED":
        return f"{r['id']} BLOCKED by core since {r['since']}: {r['blocked_by_core']}"
    if r["verdict"] == "REFUSED":
        return f"{r['id']} REFUSED: " + "; ".join(r["reasons"])
    interval = f" [{_num(r['lo'])}, {_num(r['hi'])}]" if r.get("lo") is not None else ""
    others = "".join(f" {k}={_num(v)}" for k, v in r["others"].items())
    return (f"{r['id']} {r['verdict']} n={r['progress']} {r['metric']}={_num(r['value'])}"
            f"{interval} baseline={_num(r['baseline'])}{others} keep_if '{r['keep_if']}' "
            f"rollback_if '{r['rollback_if']}'")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose", help="print the baseline an open row must carry")
    p.add_argument("--metric", required=True)
    p.add_argument("--window", required=True, help="days=<n> or rows=<n>")
    p.add_argument("--commit", help="the experiment commit; the window ends at it")
    p.add_argument("--json", action="store_true")
    g = sub.add_parser("grade", help="one verdict line per open experiment")
    g.add_argument("--json", action="store_true")
    s = sub.add_parser("status", help="the folded ledger")
    s.add_argument("--lever", help="only experiments on this strategy/ path")
    s.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rpc.forbid_network("core/experiments.py reads the journals and nothing else")
    try:
        if args.cmd == "propose":
            data = propose(args.metric, args.window, commit=args.commit)
            if args.json:
                print(json.dumps(data, indent=1))
            else:
                print(f"baseline {json.dumps(data['baseline'])} for {data['metric']} over "
                      f"{json.dumps(data['window'])} ending {data['ends']} "
                      f"(n {data['n']}{', ' + data['detail'] if data['detail'] else ''})")
                print('copy into the open row: "metric": ' + json.dumps(data["metric"])
                      + ', "baseline": ' + json.dumps(data["baseline"])
                      + ', "window": ' + json.dumps(data["window"]))
            return 0
        if args.cmd == "grade":
            results = grade()
            if args.json:
                print(json.dumps(results, indent=1))
            elif not results:
                print("no open experiment")
            else:
                for r in results:
                    print(grade_line(r))
            return 1 if any(r["verdict"] == "REFUSED" for r in results) else 0
        data = status(lever=args.lever)
        if args.json:
            print(json.dumps(data, indent=1))
            return 0
        c = data["counts"]
        print(f"{c['open']} open (cap {data['max_open']}), {c['blocked']} blocked by core, "
              f"{c['closed']} closed")
        for e in data["experiments"]:
            tail = (f"{e['outcome']}: {e['lesson']}" if e["state"] == "closed"
                    else f"blocked: {e['blocked_by_core']}" if e["state"] == "blocked"
                    else f"{e['metric']} {json.dumps(e['window'])} last {e['last_verdict'] or '-'}")
            print(f"  {e['id']} {e['state']:7s} {', '.join(e['lever'] or [])}  {tail}")
        for problem in data["ledger_problems"]:
            print(f"  problem: {problem}")
        return 0
    except ExperimentError as err:
        print(f"experiments: {err}", file=sys.stderr)
        return 2 if args.cmd != "propose" else 1


if __name__ == "__main__":
    raise SystemExit(main())
