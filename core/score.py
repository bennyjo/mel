"""The score: what the agent forecast against what the venues paid.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

Owns the three comparisons that are the point of the repo. For every settled
allocation, and every loss-marked one (`core/settle.py`), which scores as
what the venue could pay when it would not pay the position back:

  - `net_delta`        = realized_net_apy - forecast_net_apy. Did the agent
    beat its own prior? This is the honest metric. Profit on a rising market
    proves nothing; a forecast error of zero is skill whichever way the market
    went.
  - `advertised_delta` = realized_net_apy - advertised_apy. What the venue's
    own number was worth once gas, decay and impermanent loss were paid.
  - `idle_delta`       = realized_net_apy - the idle baseline. Was moving the
    money better than leaving it alone? Idle rows in the ledger are the
    empirical baseline; with none settled yet the baseline is zero, which is
    what holding USDG on 4663 earns.

Reported overall, per venue, and per kind. Idle rows are scored like any other
venue and are also excluded from the `deployed` group, so the two questions
"how good are the forecasts" and "was moving worth it" stay separable.

A second section scores the stake-free forecast ledger,
`journal/forecasts.jsonl` (see `core/forecast.py`). It is kept apart because
its rates are gross, on a notional position, with nothing charged: not one of
its numbers is comparable with the allocation score. Per forecaster and per
venue kind it reports the mean absolute error of every predictor (the agent's
forecast and the four mechanical baselines), the agent against the best
baseline, and the agent's win rate against each baseline.

Rates are autocorrelated and venues of one kind share interest-rate models,
so rows are not independent. Every statistic here is clustered by (venue
kind, UTC day of entry): the interval is a cluster-robust t interval on
G - 1 degrees of freedom, and `n_eff` is n divided by the design effect, the
number of independent rows the clustered sample is worth. Below
`MIN_CLUSTERS` clusters or `MIN_EFFECTIVE_N` effective rows the report says
it is too few to say, whatever the point estimate.

The best baseline is not chosen on the agent's own rows, which would pick
whichever baseline happened to be luckiest there. It is the baseline with the
lowest mean absolute error on the settled baseline rows of the same kind and
horizon, then of the same kind; only when there are none does it fall back to
the agent rows themselves, and the report says so. A row whose best baseline
is null is compared with the next in that ranking.

Both reports are derived from the two journal files and nothing else. They
never read the chain: an outcome is settled once, by `core/settle.py` or
`core/forecast.py`, and re-reading state here would invite a second, quieter
answer. `--offline` makes that a property the process enforces rather than a
promise, and CI passes it.

Commands:
    python3 core/score.py
    python3 core/score.py --json --offline
    python3 core/score.py --venue 0x9d53
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import forecast, ledger, rpc

# Holding USDG on 4663 earns nothing: there is no rebasing balance and no
# native staking. The baseline is measured from settled idle rows when the
# ledger has any, so the constant is a fallback and not an assumption.
IDLE_BASELINE_APY = 0.0


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _sum_usdg(rows: list[dict], field: str) -> str:
    total = sum((Decimal(str(r[field])) for r in rows if r.get(field) is not None),
                Decimal(0))
    return ledger.usdg_amount(total)


def idle_baseline(rows: list[dict]) -> tuple[float, str]:
    """The rate doing nothing actually returned, and where that number comes
    from. Settled idle rows are the measurement; the constant is the fallback
    for a ledger that has not settled one yet."""
    idle = [r for r in rows if r.get("kind") == "idle" and r.get("status") == "settled"]
    if idle:
        return (statistics.fmean(float(r["realized_net_apy"]) for r in idle),
                f"{len(idle)} settled idle rows")
    return IDLE_BASELINE_APY, "no settled idle row yet; USDG held on 4663 earns nothing"


def deltas(row: dict, baseline: float) -> dict:
    """The three comparisons for one settled row."""
    realized = float(row["realized_net_apy"])
    advertised = row.get("advertised_apy")
    return {"net_delta": realized - float(row["forecast_net_apy"]),
            "advertised_delta": None if advertised is None else realized - float(advertised),
            "idle_delta": realized - baseline}


def group(rows: list[dict], baseline: float) -> dict:
    """Aggregate one set of settled rows.

    Deltas are averaged per row, not per USDG: the question is whether the
    agent's judgment is any good, and a large allocation is not a better test
    of judgment than a small one. USDG totals are reported beside them so the
    money is visible too.
    """
    settled = [r for r in rows if r.get("status") in ledger.SCORED_STATUSES]
    out = {"n": len(settled),
           "n_open": sum(1 for r in rows if r.get("status") == "open"),
           "n_marked": sum(1 for r in rows if r.get("status") == "marked"),
           "n_void": sum(1 for r in rows if r.get("status") == "void"),
           "size_usdg": _sum_usdg(settled, "size_usdg"),
           "realized_usdg": _sum_usdg(settled, "realized_usdg"),
           "cost_usdg": ledger.usdg_amount(
               sum((Decimal(str(r.get("entry_cost_usdg", "0.000000")))
                    + Decimal(str(r.get("exit_cost_usdg", "0.000000")))
                    for r in settled), Decimal(0))),
           "forecast_usdg": _sum_usdg(settled, "forecast_net_usdg")}
    if not settled:
        return out

    d = [deltas(r, baseline) for r in settled]
    out.update({
        "advertised_apy": _mean([float(r["advertised_apy"]) for r in settled
                                 if r.get("advertised_apy") is not None]),
        "forecast_net_apy": _mean([float(r["forecast_net_apy"]) for r in settled]),
        "realized_net_apy": _mean([float(r["realized_net_apy"]) for r in settled]),
        "net_delta": _mean([x["net_delta"] for x in d]),
        "advertised_delta": _mean([x["advertised_delta"] for x in d
                                   if x["advertised_delta"] is not None]),
        "idle_delta": _mean([x["idle_delta"] for x in d]),
        "abs_net_delta": _mean([abs(x["net_delta"]) for x in d]),
        "beat_forecast": sum(1 for x in d if x["net_delta"] >= 0),
        "beat_idle": sum(1 for x in d if x["idle_delta"] > 0),
        "profitable": sum(1 for r in settled if Decimal(str(r["realized_usdg"])) > 0),
        "mean_hold_minutes": _mean([float(r.get("hold_minutes") or 0) for r in settled]),
    })
    return out


def report(rows: list[dict]) -> dict:
    """The whole report: overall, deployed, per kind, per venue, and the open
    book that has not settled yet."""
    baseline, source = idle_baseline(rows)
    venues_seen = sorted({r["venue"] for r in rows})
    kinds = sorted({r["kind"] for r in rows})
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ledger": str(ledger.LEDGER_PATH.relative_to(ROOT)),
        "rows": len(rows),
        "idle_baseline_apy": baseline,
        "idle_baseline_source": source,
        "overall": group(rows, baseline),
        "deployed": group([r for r in rows if r["kind"] != "idle"], baseline),
        "by_kind": {k: group([r for r in rows if r["kind"] == k], baseline)
                    for k in kinds},
        "by_venue": {v: group([r for r in rows if r["venue"] == v], baseline)
                     for v in venues_seen},
        "open": [{"id": r["id"], "venue": r["venue"], "kind": r["kind"],
                  "size_usdg": r["size_usdg"], "ts": r["ts"],
                  "forecast_net_apy": r["forecast_net_apy"],
                  "forecast_horizon_min": r["forecast_horizon_min"],
                  "matures_ts": r["entry_ts"] + r["forecast_horizon_min"] * 60}
                 for r in ledger.open_rows(rows)],
    }


# --------------------------------------------------------------------------
# The forecast ledger
# --------------------------------------------------------------------------

# A verdict needs this many (kind, day) clusters and this many effective rows.
# Below either, a cluster-robust interval is a sketch: with few clusters its
# own variance estimate is too noisy to trust, and t on G - 1 degrees of
# freedom only partly pays for that.
MIN_CLUSTERS = 10
MIN_EFFECTIVE_N = 30

# The all-time verdict can't show learning: new rows only dilute old ones. These
# age bands, in days before now by entry time, can. They leave out Uniswap v3
# rows, whose price-driven rates would swamp a short window.
WINDOWS_DAYS = ((0, 7), (7, 14), (14, 28))

# The two-sided 95% quantile of Student's t by degrees of freedom. Between two
# listed values the lower one's is used, which widens the interval.
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
        14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042, 40: 2.021,
        60: 2.000, 120: 1.980}


def t975(df: int) -> float:
    return T975[max(k for k in T975 if k <= max(df, 1))]


def cluster_of(row: dict) -> str:
    """The unit rows are not independent within: one venue kind on one UTC
    day of entry."""
    return f"{row['kind']}:{time.strftime('%Y-%m-%d', time.gmtime(int(row['entry_ts'])))}"


def clustered_mean(values: list[float], clusters: list[str]) -> dict:
    """A mean, its cluster-robust 95% interval, and its effective n.

    The variance is the CR1 sandwich: the squared per-cluster sums of the
    residuals, scaled by G / (G - 1), with t on G - 1 degrees of freedom. The
    effective n is n over the design effect, the clustered variance of the
    mean against the variance the same rows would give if independent. It is
    G for G clusters of identical rows and n for clusters of one, and never
    more than n. One cluster has no interval at all."""
    n = len(values)
    out = {"n": n, "clusters": len(set(clusters)), "mean": None, "se": None,
           "lo": None, "hi": None, "n_eff": 0.0}
    if not n:
        return out
    mean = statistics.fmean(values)
    sums: dict[str, float] = {}
    for value, cluster in zip(values, clusters):
        sums[cluster] = sums.get(cluster, 0.0) + value - mean
    between = sum(s * s for s in sums.values())
    within = sum((v - mean) * (v - mean) for v in values)
    g = len(sums)
    out["mean"] = mean
    out["n_eff"] = float(n) if between <= 0 else min(float(n), n * within / between)
    if g >= 2:
        se = math.sqrt(g / (g - 1) * between) / n
        half = t975(g - 1) * se
        out.update(se=se, lo=mean - half, hi=mean + half)
    return out


def enough(stat: dict) -> bool:
    return stat["clusters"] >= MIN_CLUSTERS and stat["n_eff"] >= MIN_EFFECTIVE_N


def verdict(stat: dict) -> str:
    """What the agent-against-best-baseline interval says, in words."""
    if not stat["n"]:
        return "no agent row has settled beside a baseline, so there is nothing to compare"
    if not enough(stat):
        return (f"too few to say: {stat['n']} rows in {stat['clusters']} (kind, day) "
                f"clusters, effective n {stat['n_eff']:.1f}. A verdict needs "
                f"{MIN_CLUSTERS} clusters and an effective n of {MIN_EFFECTIVE_N}")
    if stat["hi"] < 0:
        return "the agent's error is smaller than the best baseline's"
    if stat["lo"] > 0:
        return ("the best baseline's error is smaller than the agent's: a mechanical "
                "rule forecasts these rates better")
    return "the interval cannot tell the agent from the best baseline"


def _errors(rows: list[dict], name: str) -> tuple[list[float], list[str]]:
    """One predictor's signed errors on the rows where it spoke, and their clusters."""
    pairs = [(float(r["errors"][name]), cluster_of(r)) for r in rows
             if (r.get("errors") or {}).get(name) is not None]
    return [e for e, _ in pairs], [c for _, c in pairs]


def rank_baselines(rows: list[dict]) -> list[str]:
    """Baselines by mean absolute error on `rows`, lowest first. One that is
    null on more than half of them ranks after every one that is not, so a
    baseline cannot rank first by speaking only on the easy rows. One null on
    every row does not rank."""
    scored = []
    for name in forecast.BASELINES:
        errs, _ = _errors(rows, name)
        if errs:
            scored.append((len(errs) * 2 < len(rows),
                           statistics.fmean(abs(e) for e in errs), name))
    return [name for *_, name in sorted(scored)]


def best_baselines(settled: list[dict]) -> dict[tuple[str, int], dict]:
    """The best-baseline ranking for each (kind, horizon) an agent row settled
    at, and the rows it was ranked on: baseline rows of the kind and horizon
    when there are enough, else baseline rows of the kind, else, in-sample,
    the agent's own rows of the kind."""
    base = [r for r in settled if r["forecaster"] == "baseline"]
    agent = [r for r in settled if r["forecaster"] == "agent"]
    out: dict[tuple[str, int], dict] = {}
    for kind, horizon in sorted({(r["kind"], int(r["horizon_min"])) for r in agent}):
        cell = [r for r in base if r["kind"] == kind and int(r["horizon_min"]) == horizon]
        pools = [(f"baseline rows of the kind at {horizon} min",
                  cell if len(cell) >= MIN_EFFECTIVE_N else []),
                 ("baseline rows of the kind at every horizon",
                  [r for r in base if r["kind"] == kind]),
                 ("the agent's own rows of the kind, in-sample, because no baseline "
                  "row of the kind has settled", [r for r in agent if r["kind"] == kind])]
        for source, pool in pools:
            rank = rank_baselines(pool)
            if rank:
                out[(kind, horizon)] = {"rank": rank, "source": source, "n": len(pool)}
                break
    return out


def versus(rows: list[dict], ranks: dict[tuple[str, int], dict]) -> dict:
    """The agent against the best baseline and against each baseline, on the
    settled agent rows where both spoke. The difference is |agent error| -
    |baseline error|: negative means the agent was closer. A win is the agent
    strictly closer, a tie half a win."""
    out: dict[str, dict] = {}
    for name in ("best",) + forecast.BASELINES:
        diffs, wins, clusters, agent_abs, base_abs = [], [], [], [], []
        used: dict[str, int] = {}
        for r in rows:
            errors = r.get("errors") or {}
            if errors.get("agent") is None:
                continue
            if name == "best":
                rank = (ranks.get((r["kind"], int(r["horizon_min"]))) or {}).get("rank", [])
                pick = next((b for b in rank if errors.get(b) is not None), None)
            else:
                pick = name if errors.get(name) is not None else None
            if pick is None:
                continue
            used[pick] = used.get(pick, 0) + 1
            a, b = abs(float(errors["agent"])), abs(float(errors[pick]))
            diffs.append(a - b)
            wins.append(1.0 if a < b else 0.5 if a == b else 0.0)
            clusters.append(cluster_of(r))
            agent_abs.append(a)
            base_abs.append(b)
        out[name] = {"diff": clustered_mean(diffs, clusters),
                     "win_rate": clustered_mean(wins, clusters),
                     "mae_agent": statistics.fmean(agent_abs) if agent_abs else None,
                     "mae_baseline": statistics.fmean(base_abs) if base_abs else None}
        if name == "best":
            out[name]["used"] = used
            out[name]["verdict"] = verdict(out[name]["diff"])
    return out


def forecast_group(rows: list[dict], ranks: dict[tuple[str, int], dict]) -> dict:
    """One set of forecast rows: the realized rate, each predictor's mean
    absolute error and bias (mean signed error, realized minus predicted, so
    negative means it promised more than the venue paid), and for agent rows
    the comparison with the baselines."""
    settled = [r for r in rows if r.get("status") == "settled"]
    clusters = [cluster_of(r) for r in settled]
    out = {"n": len(settled),
           "n_open": sum(1 for r in rows if r.get("status") == "open"),
           "n_void": sum(1 for r in rows if r.get("status") == "void"),
           "clusters": len(set(clusters)),
           "realized_gross_apy": clustered_mean(
               [float(r["realized_gross_apy"]) for r in settled], clusters),
           "mae": {}, "bias": {}}
    for name in forecast.PREDICTORS:
        errs, cl = _errors(settled, name)
        if errs:
            out["mae"][name] = clustered_mean([abs(e) for e in errs], cl)
            out["bias"][name] = clustered_mean(errs, cl)
    if "agent" in out["mae"]:
        out["versus"] = versus(settled, ranks)
    return out


def in_window(row: dict, days: tuple[int, int], now: float) -> bool:
    age = now - int(row["entry_ts"])
    return (row["kind"] != "uniswap_v3_pool"
            and days[0] * 86_400 <= age < days[1] * 86_400)


def forecast_report(rows: list[dict], now: float | None = None) -> dict:
    """The forecast section: per forecaster, overall, per kind, per horizon
    and per recent window."""
    now = time.time() if now is None else now
    settled = [r for r in rows if r.get("status") == "settled"]
    ranks = best_baselines(settled)
    out = {"ledger": str(forecast.FORECASTS_PATH.relative_to(ROOT)),
           "rows": len(rows),
           "by_status": {s: sum(1 for r in rows if r.get("status") == s)
                         for s in forecast.STATUSES},
           "cluster": "(venue kind, UTC day of entry)",
           "min_clusters": MIN_CLUSTERS, "min_effective_n": MIN_EFFECTIVE_N,
           "best_baseline": {f"{k}:{h}": v for (k, h), v in ranks.items()},
           "by_forecaster": {}}
    for who in forecast.FORECASTERS:
        mine = [r for r in rows if r.get("forecaster") == who]
        if not mine:
            continue
        out["by_forecaster"][who] = {
            "all": forecast_group(mine, ranks),
            "by_kind": {k: forecast_group([r for r in mine if r["kind"] == k], ranks)
                        for k in sorted({r["kind"] for r in mine})},
            "by_horizon": {str(h): forecast_group([r for r in mine
                                                   if int(r["horizon_min"]) == h], ranks)
                           for h in sorted({int(r["horizon_min"]) for r in mine})},
            "by_window": {f"{lo}-{hi} d": forecast_group([r for r in mine
                                                          if in_window(r, (lo, hi), now)], ranks)
                          for lo, hi in WINDOWS_DAYS}}
    return out


# --------------------------------------------------------------------------
# Text report
# --------------------------------------------------------------------------

def _pct(value: object, width: int = 8, sign: bool = True) -> str:
    if value is None:
        return "-".rjust(width)
    return (f"{float(value) * 100:+.2f}%" if sign
            else f"{float(value) * 100:.2f}%").rjust(width)


def _line(name: str, g: dict) -> str:
    return (f"{name[:26]:26s} {g['n']:>3d} {g['realized_usdg']:>11s} "
            f"{_pct(g.get('advertised_apy'))} {_pct(g.get('forecast_net_apy'))} "
            f"{_pct(g.get('realized_net_apy'))} {_pct(g.get('net_delta'))} "
            f"{_pct(g.get('advertised_delta'))} {_pct(g.get('idle_delta'))}")


def print_report(data: dict) -> None:
    head = (f"{'group':26s} {'n':>3s} {'realized USDG':>11s} {'advert':>8s} "
            f"{'forecast':>8s} {'realized':>8s} {'net_d':>8s} {'adv_d':>8s} {'idle_d':>8s}")
    print(f"Mel score, {data['generated']}, {data['rows']} ledger rows")
    print(f"idle baseline {data['idle_baseline_apy'] * 100:.2f}% "
          f"({data['idle_baseline_source']})\n")
    if not data["overall"]["n"]:
        print("nothing has settled yet"
              + (", so there is nothing to score" if not data["open"] else ""))
    else:
        print(head)
        print(_line("OVERALL", data["overall"]))
        print(_line("deployed (not idle)", data["deployed"]))
        print()
        for kind, g in sorted(data["by_kind"].items()):
            if g["n"]:
                print(_line(f"kind {kind}", g))
        for venue, g in sorted(data["by_venue"].items()):
            if g["n"]:
                print(_line(f"  {venue}", g))
        o = data["overall"]
        print(f"\nforecast beaten in {o['beat_forecast']}/{o['n']}, "
              f"idle beaten in {o['beat_idle']}/{o['n']}, "
              f"profitable {o['profitable']}/{o['n']}, "
              f"mean |net_delta| {o['abs_net_delta'] * 100:.2f}%, "
              f"costs paid {o['cost_usdg']} USDG")
        if o["n_marked"]:
            print(f"{o['n_marked']} of the {o['n']} are loss-marked, not exited, and "
                  "re-checked every settle run")

    if data["open"]:
        print(f"\n{len(data['open'])} open:")
        now = time.time()
        for r in data["open"]:
            left = (r["matures_ts"] - now) / 60
            due = f"{left:+.0f} min" if abs(left) < 1440 else f"{left / 1440:+.1f} d"
            print(f"  {r['id']:7s} {r['kind'][:20]:20s} {r['venue'][:14]:14s} "
                  f"{r['size_usdg']:>9s} forecast {_pct(r['forecast_net_apy'])} "
                  f"matures {due}")


LABELS = {"agent": "agent", "advertised": "advert", "trailing_24h": "t24h",
          "trailing_7d": "t7d", "haircut": "haircut", "best": "best"}


def _share(value: float) -> str:
    return f"{value * 100:.0f}%"


def _groups(block: dict) -> list[tuple[str, dict]]:
    return ([("all", block["all"])]
            + [(f"kind {k}", g) for k, g in sorted(block["by_kind"].items())]
            + [(f"horizon {h} min", g) for h, g in sorted(block["by_horizon"].items(),
                                                         key=lambda kv: int(kv[0]))]
            + [(f"lending, {w} ago", g) for w, g in block["by_window"].items()])


def print_forecasts(data: dict) -> None:
    s = data["by_status"]
    print(f"\nForecast ledger ({data['ledger']}): {s['settled']} settled, "
          f"{s['open']} open, {s['void']} void")
    print("Gross rates on a notional position with nothing charged, so not comparable\n"
          "with the allocation score above.")
    if not s["settled"]:
        print("nothing has settled yet, so there is nothing to score")
        return
    print(f"Clustered by {data['cluster']}. Intervals are 95% cluster-robust t,\n"
          "and n_eff is the effective n of the realized rate.\n")
    print("mean absolute error of each predictor")
    print(f"{'forecaster':10s} {'group':24s} {'n':>4s} {'clus':>4s} {'n_eff':>6s} "
          f"{'realized':>8s} " + " ".join(f"{LABELS[p]:>8s}" for p in forecast.PREDICTORS))
    for who, block in data["by_forecaster"].items():
        for name, g in _groups(block):
            if not g["n"]:
                continue
            r = g["realized_gross_apy"]
            print(f"{who:10s} {name[:24]:24s} {g['n']:>4d} {g['clusters']:>4d} "
                  f"{r['n_eff']:>6.1f} {_pct(r['mean'])} "
                  + " ".join(_pct((g["mae"].get(p) or {}).get("mean"), sign=False)
                             for p in forecast.PREDICTORS))

    if data["best_baseline"]:
        print("\nbest baseline, lowest mean absolute error first:")
        for key, v in data["best_baseline"].items():
            print(f"  {key:28s} {' > '.join(LABELS[b] for b in v['rank'])} "
                  f"({v['source']}, {v['n']} rows)")

    agent = data["by_forecaster"].get("agent")
    if not agent or not agent["all"]["n"]:
        print("\nno agent forecast has settled yet")
        return
    print("\nthe agent against the best baseline: mean of |agent error| - |baseline "
          "error|, negative is the agent closer")
    for name, g in _groups(agent):
        v = g.get("versus")
        if not v:
            continue
        d = v["best"]["diff"]
        interval = (f"[{_pct(d['lo'], 0)}, {_pct(d['hi'], 0)}]" if d["lo"] is not None
                    else "[no interval with one cluster]")
        wins = ", ".join(f"{LABELS[b]} {_share(v[b]['win_rate']['mean'])}"
                         for b in ("best",) + forecast.BASELINES if v[b]["win_rate"]["n"])
        print(f"  {name[:24]:24s} n {d['n']:>3d} clus {d['clusters']:>3d} "
              f"n_eff {d['n_eff']:>5.1f}  {_pct(d['mean'])} {interval}  wins vs {wins}")
        print(f"  {'':24s} {v['best']['verdict']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="refuse any network use. The report never needs it; "
                         "this makes that a checked property. CI passes it.")
    ap.add_argument("--venue", help="score one venue (address prefix)")
    ap.add_argument("--kind")
    args = ap.parse_args(argv)

    if args.offline:
        rpc.forbid_network("core/score.py --offline reads the ledger and nothing else")

    rows, frows = ledger.load(), forecast.load()
    if args.venue:
        rows = [r for r in rows if r["venue"].lower().startswith(args.venue.lower())]
        frows = [r for r in frows if r["venue"].lower().startswith(args.venue.lower())]
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
        frows = [r for r in frows if r["kind"] == args.kind]
    data = report(rows)
    data["forecasts"] = forecast_report(frows)
    if args.json:
        print(json.dumps(data, indent=1, sort_keys=True))
    else:
        print_report(data)
        print_forecasts(data["forecasts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
