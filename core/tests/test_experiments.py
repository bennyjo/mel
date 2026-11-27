"""The experiment grader and its validate checks, on a throwaway git repository.

PROTECTED ENGINE FILE. The allocating agent may not edit this file.

Every experiment here is a real commit in a temporary repository with a
pinned timestamp, so the sealed cutoff (the forward window starts at the
commit) is tested the way the grader reads it. Nothing touches the network.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import experiments, rpc, validate

DAY = 86_400
T0 = 1_790_000_000 - 1_790_000_000 % DAY
C = T0 + 10 * DAY           # the experiment commit's timestamp
RISK = {"max_allocation_usdg": 250.0, "min_expected_net_bps": 5.0,
        "max_round_trip_bps": 25.0}
RULE = "def forecast(s):\n    return s['trailing_24h']\n"
BOUNDARY = ROOT / ".github" / "scripts" / "boundary.sh"


def setUpModule() -> None:
    rpc.forbid_network("the grader reads the journals and nothing else")


def iso(ts: float) -> str:
    return experiments.iso(ts)


class Repo:
    """A git repository with strategy files, journals and an experiment ledger."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.base = self.commit({
            "strategy/risk.json": json.dumps(RISK),
            "strategy/schedule.json": json.dumps({"full_cycle_hours_utc": [1, 13]}),
            "strategy/playbook.md": "# Playbook\n",
            "strategy/forecast_rule.py": RULE,
            "strategy/venue-filters.json": "{}",
            "core/x.py": "x = 1\n",
        }, "seed", T0)
        self.forecasts: list[dict] = []
        self.allocations: list[dict] = []
        self.usage: list[dict] = []

    def git(self, *args: str, ts: int | None = None) -> str:
        env = dict(os.environ)
        if ts is not None:
            env.update(GIT_AUTHOR_DATE=f"@{ts} +0000", GIT_COMMITTER_DATE=f"@{ts} +0000")
        out = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True,
                             text=True, env=env)
        if out.returncode:
            raise AssertionError(out.stderr)
        return out.stdout.strip()

    def commit(self, files: dict[str, str], message: str, ts: int) -> str:
        for name, body in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        self.git("add", "--", *files)
        self.git("commit", "-q", "-m", message, ts=ts)
        return self.git("rev-parse", "HEAD")

    def experiment(self, ts: int = C, files: dict[str, str] | None = None) -> str:
        return self.commit(files or {"strategy/playbook.md": f"# Playbook\nrows {ts}\n"},
                           "experiment: E change", ts)

    def ledger(self, rows: list[dict]) -> None:
        (self.path / experiments.LEDGER_REL).write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    def flush(self) -> None:
        journal = self.path / "journal"
        journal.mkdir(exist_ok=True)
        for name, rows in (("forecasts.jsonl", self.forecasts),
                           ("allocations.jsonl", self.allocations),
                           ("rpc-usage.jsonl", self.usage)):
            (journal / name).write_text("".join(json.dumps(r) + "\n" for r in rows))

    def agent_rows(self, start: int, count: int, step: int = 3600) -> None:
        for i in range(count):
            self.forecasts.append({"id": f"f{len(self.forecasts):06d}", "forecaster": "agent",
                                   "ts": iso(start + i * step), "status": "open",
                                   "kind": "morpho_market"})


def opened(eid: str, commit: str | None, ts: int = C + 600, **kw) -> dict:
    row = {"event": "open", "id": eid, "ts": iso(ts), "hypothesis": "more rows",
           "lever": ["strategy/playbook.md#rows"], "commit": commit,
           "metric": "agent_rows_per_day", "baseline": 2.0, "window": {"days": 4},
           "keep_if": ">= 5", "rollback_if": "< 2", "prior": [], "blocked_by_core": None}
    row.update(kw)
    return row


def closed(eid: str, ts: int, outcome: str = "kept") -> dict:
    return {"event": "close", "id": eid, "ts": iso(ts), "outcome": outcome,
            "lesson": "one line"}


class Base(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def problems(self) -> tuple[list[str], list[str]]:
        return experiments.ledger_problems(self.repo.path)

    def assertFails(self, needle: str) -> None:
        failures, _ = self.problems()
        self.assertTrue(any(needle in f for f in failures), failures)

    def assertClean(self) -> None:
        failures, _ = self.problems()
        self.assertEqual(failures, [])


# --------------------------------------------------------------------------
# The ledger rules (core/validate.py fails on these)
# --------------------------------------------------------------------------

class TestLedgerRules(Base):

    def test_a_well_formed_open_row_folds_clean(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha)])
        self.assertClean()

    def test_no_ledger_is_no_failure(self):
        rep = validate.Report()
        validate.check_experiments(rep, self.repo.path)
        self.assertEqual(rep.failures, [])
        self.assertIn("does not exist yet", rep.notes[0])

    def test_validate_reports_ledger_failures(self):
        self.repo.ledger([opened("E-2026-11-17-a", "deadbeef")])
        rep = validate.Report()
        validate.check_experiments(rep, self.repo.path)
        self.assertTrue(any("does not resolve" in f for f in rep.failures), rep.failures)

    def test_validate_fails_a_ledger_that_is_not_json(self):
        (self.repo.path / experiments.LEDGER_REL).write_text("{not json\n")
        rep = validate.Report()
        validate.check_experiments(rep, self.repo.path)
        self.assertTrue(any("is not JSON" in f for f in rep.failures), rep.failures)

    def test_at_most_three_open(self):
        rows = []
        for n, letter in enumerate("abcd"):
            sha = self.repo.experiment(C + n * 60)
            rows.append(opened(f"E-2026-11-17-{letter}", sha, C + n * 60 + 30,
                               lever=[f"strategy/playbook.md#s{letter}"]))
        self.repo.ledger(rows)
        self.assertFails("3 experiments are already open")

    def test_a_blocked_by_core_row_does_not_count_and_may_name_core(self):
        rows = [{"event": "open", "id": "E-2026-11-16-z", "ts": iso(C - 60),
                 "hypothesis": "fix the settle", "lever": ["core/settle.py"],
                 "commit": None, "prior": [], "blocked_by_core": "core/settle.py v3 settle"}]
        for n, letter in enumerate("abc"):
            sha = self.repo.experiment(C + n * 60)
            rows.append(opened(f"E-2026-11-17-{letter}", sha, C + n * 60 + 30,
                               lever=[f"strategy/playbook.md#s{letter}"]))
        self.repo.ledger(rows)
        self.assertClean()

    def test_a_blocked_by_core_row_has_no_commit(self):
        sha = self.repo.experiment()
        self.repo.ledger([{"event": "open", "id": "E-2026-11-17-a", "ts": iso(C + 60),
                           "hypothesis": "h", "lever": ["core/x.py"], "commit": sha,
                           "prior": [], "blocked_by_core": "needs core"}])
        self.assertFails("has no commit: nothing was changed")

    def test_a_lever_outside_strategy_is_refused(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha, lever=["core/x.py"])])
        self.assertFails("is not an agent-owned file under strategy/")

    def test_the_ledger_is_not_a_lever(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha, lever=[experiments.LEDGER_REL])])
        self.assertFails("is not an agent-owned file")

    def test_two_open_experiments_cannot_share_a_lever(self):
        a, b = self.repo.experiment(C), self.repo.experiment(C + 60)
        self.repo.ledger([opened("E-2026-11-17-a", a, C + 30),
                          opened("E-2026-11-17-b", b, C + 90,
                                 lever=["strategy/playbook.md"])])
        self.assertFails("is already held by open experiment E-2026-11-17-a")

    def test_different_sections_of_one_file_are_different_levers(self):
        a, b = self.repo.experiment(C), self.repo.experiment(C + 60)
        self.repo.ledger([opened("E-2026-11-17-a", a, C + 30),
                          opened("E-2026-11-17-b", b, C + 90,
                                 lever=["strategy/playbook.md#other"])])
        self.assertClean()

    def test_a_closed_experiment_frees_its_lever(self):
        a, b = self.repo.experiment(C), self.repo.experiment(C + 2 * DAY)
        self.repo.ledger([opened("E-2026-11-17-a", a, C + 30), closed("E-2026-11-17-a", C + DAY),
                          opened("E-2026-11-19-a", b, C + 2 * DAY + 30)])
        self.assertClean()

    def test_a_lever_rolled_back_within_30_days_needs_prior(self):
        a, b = self.repo.experiment(C), self.repo.experiment(C + 2 * DAY)
        rows = [opened("E-2026-11-17-a", a, C + 30),
                closed("E-2026-11-17-a", C + DAY, "rolled_back"),
                opened("E-2026-11-19-a", b, C + 2 * DAY + 30)]
        self.repo.ledger(rows)
        self.assertFails("rolled back in E-2026-11-17-a within 30 days")
        rows[-1]["prior"] = ["E-2026-11-17-a"]
        self.repo.ledger(rows)
        self.assertClean()

    def test_a_rollback_older_than_30_days_needs_no_prior(self):
        a, b = self.repo.experiment(C), self.repo.experiment(C + 40 * DAY)
        self.repo.ledger([opened("E-2026-11-17-a", a, C + 30),
                          closed("E-2026-11-17-a", C + DAY, "rolled_back"),
                          opened("E-2026-12-27-a", b, C + 40 * DAY + 30)])
        self.assertClean()

    def test_the_commit_must_be_an_ancestor_of_head(self):
        sha = self.repo.experiment()
        self.repo.git("checkout", "-q", "-b", "other", self.repo.base)
        self.repo.ledger([opened("E-2026-11-17-a", sha)])
        self.assertFails("is not an ancestor of HEAD")

    def test_the_commit_may_not_touch_the_ledger(self):
        sha = self.repo.commit({experiments.LEDGER_REL: "", "strategy/playbook.md": "x\n"},
                               "experiment: both", C)
        self.repo.ledger([opened("E-2026-11-17-a", sha)])
        self.assertFails("never the ledger")

    def test_the_commit_may_not_touch_a_protected_path(self):
        sha = self.repo.commit({"core/x.py": "x = 2\n"}, "experiment: core", C)
        self.repo.ledger([opened("E-2026-11-17-a", sha)])
        self.assertFails("touches core/x.py")

    def test_the_commit_touches_at_most_two_files(self):
        sha = self.repo.commit({"strategy/playbook.md": "x\n", "strategy/venue-filters.json":
                                '{"a": 1}', "strategy/schedule.json": "{}"},
                               "experiment: three", C)
        self.repo.ledger([opened("E-2026-11-17-a", sha)])
        self.assertFails("touches 3 files")

    def test_the_commit_may_not_loosen_risk(self):
        loose = dict(RISK, max_allocation_usdg=500.0)
        sha = self.repo.commit({"strategy/risk.json": json.dumps(loose)}, "experiment: x", C)
        self.repo.ledger([opened("E-2026-11-17-a", sha, lever=["strategy/risk.json"])])
        self.assertFails("max_allocation_usdg 250 -> 500")

    def test_the_commit_may_not_drop_a_risk_cap(self):
        loose = {k: v for k, v in RISK.items() if k != "max_round_trip_bps"}
        sha = self.repo.commit({"strategy/risk.json": json.dumps(loose)}, "experiment: x", C)
        self.repo.ledger([opened("E-2026-11-17-a", sha, lever=["strategy/risk.json"])])
        self.assertFails("max_round_trip_bps removed")

    def test_the_commit_may_tighten_risk(self):
        tight = dict(RISK, max_allocation_usdg=200.0, min_expected_net_bps=8.0)
        sha = self.repo.commit({"strategy/risk.json": json.dumps(tight)}, "experiment: x", C)
        self.repo.ledger([opened("E-2026-11-17-a", sha, lever=["strategy/risk.json"])])
        self.assertClean()

    def test_the_metric_must_be_in_the_catalog(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha, metric="vibes")])
        self.assertFails("is not in the catalog")

    def test_a_bar_must_parse_and_name_catalog_metrics(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha, keep_if="at least 5")])
        self.assertFails("is not '[metric] <op> <number>'")
        self.repo.ledger([opened("E-2026-11-17-a", sha, keep_if=">= 5 and vibes > 1")])
        self.assertFails("vibes is not in the catalog")

    def test_a_rate_metric_takes_no_rows_window(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha, window={"rows": 20})])
        self.assertFails("takes a window of days, not rows")

    def test_a_window_must_be_positive(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha, window={"days": 0})])
        self.assertFails("n above zero")

    def test_rows_must_be_in_time_order(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha, C + 600),
                          {"event": "verdict", "id": "E-2026-11-17-a", "ts": iso(C),
                           "verdict": "COLLECTING", "value": 1, "n": 1,
                           "source": "core/experiments.py grade"}])
        self.assertFails("earlier than the row before")

    def test_a_hand_verdict_is_refused(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha),
                          {"event": "verdict", "id": "E-2026-11-17-a", "ts": iso(C + DAY),
                           "verdict": "KEEP", "value": 9, "n": 4, "source": "my tally"}])
        self.assertFails("never from a hand tally")

    def test_close_rows_are_checked(self):
        sha = self.repo.experiment()
        self.repo.ledger([opened("E-2026-11-17-a", sha), closed("E-2026-11-17-a", C + DAY, "won")])
        self.assertFails("outcome must be one of")
        self.repo.ledger([opened("E-2026-11-17-a", sha), closed("E-2026-11-17-a", C + DAY),
                          closed("E-2026-11-17-a", C + 2 * DAY)])
        self.assertFails("close after E-2026-11-17-a was closed")
        self.repo.ledger([closed("E-2026-11-17-a", C + DAY)])
        self.assertFails("never opened")

    def test_a_blocked_row_closes_unblocked_or_withdrawn(self):
        row = {"event": "open", "id": "E-2026-11-17-a", "ts": iso(C), "hypothesis": "h",
               "lever": ["core/x.py"], "commit": None, "prior": [], "blocked_by_core": "b"}
        self.repo.ledger([row, closed("E-2026-11-17-a", C + DAY, "kept")])
        self.assertFails("outcome must be one of unblocked, withdrawn")
        self.repo.ledger([row, closed("E-2026-11-17-a", C + DAY, "unblocked")])
        self.assertClean()

    def test_withdrawing_a_broken_open_row_clears_the_failure(self):
        self.repo.ledger([opened("E-2026-11-17-a", "deadbeef")])
        self.assertFails("does not resolve")
        self.repo.ledger([opened("E-2026-11-17-a", "deadbeef"),
                          closed("E-2026-11-17-a", C + DAY, "withdrawn")])
        failures, notes = self.problems()
        self.assertEqual(failures, [])
        self.assertTrue(any("closed experiment E-2026-11-17-a" in n for n in notes), notes)


# --------------------------------------------------------------------------
# propose, grade, status
# --------------------------------------------------------------------------

class TestPropose(Base):

    def test_baseline_is_the_window_before_the_commit(self):
        self.repo.agent_rows(C - 4 * DAY, 8, step=DAY // 2)     # 8 rows in the 4 days before
        self.repo.agent_rows(C - 6 * DAY, 3)                  # outside the window
        self.repo.agent_rows(C + 60, 5)                       # after the commit
        self.repo.flush()
        sha = self.repo.experiment()
        got = experiments.propose("agent_rows_per_day", "days=4", self.repo.path, sha, now=C + DAY)
        self.assertEqual(got["baseline"], 2.0)
        self.assertEqual(got["n"], 8)
        self.assertEqual(got["ends"], iso(C))
        self.assertEqual(got["window"], {"days": 4.0})

    def test_without_a_commit_the_window_ends_now(self):
        self.repo.agent_rows(C - 2 * DAY, 4, step=DAY // 4)
        self.repo.flush()
        got = experiments.propose("agent_rows_per_day", "days=2", self.repo.path, now=C)
        self.assertEqual(got["baseline"], 2.0)
        self.assertIsNone(got["commit"])

    def test_refuses_what_the_catalog_cannot_measure(self):
        with self.assertRaisesRegex(experiments.ExperimentError, "not in the catalog"):
            experiments.propose("vibes", "days=2", self.repo.path, now=C)
        with self.assertRaisesRegex(experiments.ExperimentError, "takes a window of days"):
            experiments.propose("idle_share", "rows=5", self.repo.path, now=C)
        with self.assertRaisesRegex(experiments.ExperimentError, "days=<n> or rows=<n>"):
            experiments.propose("idle_share", "days=x", self.repo.path, now=C)
        with self.assertRaisesRegex(experiments.ExperimentError, "pass --commit"):
            experiments.propose("replay_pass:strategy/forecast_rule.py", "days=2",
                                self.repo.path, now=C)

    def test_cli_prints_the_row_fields(self):
        self.repo.agent_rows(C - DAY, 3)
        self.repo.flush()
        out = io.StringIO()
        with mock.patch.object(experiments, "ROOT", self.repo.path), \
                mock.patch.object(experiments.propose, "__defaults__",
                                  (self.repo.path, None, C)), \
                contextlib.redirect_stdout(out):
            code = experiments.main(["propose", "--metric", "agent_rows_per_day",
                                     "--window", "days=1"])
        self.assertEqual(code, 0)
        self.assertIn('"baseline": 3.0', out.getvalue())


class TestGrade(Base):
    """agent_rows_per_day, baseline 2 a day, window 4 days, keep at 5 or more,
    roll back under 2."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.agent_rows(C - 4 * DAY, 8, step=DAY // 2)
        self.sha = self.repo.experiment()

    def grade(self, after: int, now: int, **kw) -> dict:
        if after:
            self.repo.agent_rows(C + 60, after, step=(4 * DAY - 120) // after)
        self.repo.flush()
        self.repo.ledger([opened("E-2026-11-17-a", self.sha, **kw)])
        results = experiments.grade(self.repo.path, now=now)
        self.assertEqual(len(results), 1)
        return results[0]

    def test_collecting_before_the_window_ends(self):
        r = self.grade(3, C + DAY)
        self.assertEqual(r["verdict"], "COLLECTING")
        self.assertEqual(r["progress"], "1.0/4 days")

    def test_no_early_rollback_before_half_the_window(self):
        r = self.grade(0, C + DAY)
        self.assertEqual(r["value"], 0.0)
        self.assertEqual(r["verdict"], "COLLECTING")

    def test_early_rollback_from_half_the_window(self):
        r = self.grade(1, C + 2 * DAY + 60)
        self.assertEqual(r["verdict"], "ROLL BACK")

    def test_keep(self):
        r = self.grade(24, C + 5 * DAY)
        self.assertEqual((r["verdict"], r["value"], r["n"]), ("KEEP", 6.0, 24))
        self.assertEqual(r["row"]["source"], "core/experiments.py grade")
        self.assertEqual(r["row"]["verdict"], "KEEP")

    def test_double_down_at_twice_the_bar(self):
        r = self.grade(40, C + 5 * DAY)                   # (10 - 2) / (5 - 2) > 2
        self.assertEqual(r["verdict"], "DOUBLE DOWN")

    def test_inconclusive_when_neither_bar_holds(self):
        r = self.grade(12, C + 5 * DAY)                   # 3 a day
        self.assertEqual(r["verdict"], "INCONCLUSIVE")

    def test_roll_back_at_the_end(self):
        r = self.grade(4, C + 5 * DAY)                    # 1 a day
        self.assertEqual(r["verdict"], "ROLL BACK")

    def test_rows_after_the_window_do_not_count(self):
        self.repo.agent_rows(C + 4 * DAY + 60, 100)
        r = self.grade(12, C + 9 * DAY)
        self.assertEqual((r["verdict"], r["n"]), ("INCONCLUSIVE", 12))

    def test_a_hand_typed_baseline_is_refused(self):
        r = self.grade(24, C + 5 * DAY, baseline=3.0)
        self.assertEqual(r["verdict"], "REFUSED")
        self.assertIn("not the 2.0 that propose computes", r["reasons"][0])

    def test_a_rule_breaking_row_is_refused_not_graded(self):
        r = self.grade(24, C + 5 * DAY, lever=["core/x.py"])
        self.assertEqual(r["verdict"], "REFUSED")

    def test_a_compound_bar_reads_the_other_metric(self):
        for ts, kind in ((C + 3600, "idle"), (C + 7200, "morpho_market")):
            self.repo.allocations.append({"id": f"a{ts}", "ts": iso(ts), "kind": kind,
                                          "size_usdg": "250.000000", "status": "open"})
        r = self.grade(24, C + 5 * DAY, keep_if=">= 5 and idle_share <= 0.4")
        self.assertEqual(r["others"], {"idle_share": 0.5})
        self.assertEqual(r["verdict"], "INCONCLUSIVE")
        r = self.grade(0, C + 5 * DAY, keep_if=">= 5 and idle_share <= 0.5")
        self.assertEqual(r["verdict"], "KEEP")

    def test_blocked_and_closed_rows_are_not_graded(self):
        self.repo.flush()
        self.repo.ledger([
            {"event": "open", "id": "E-2026-11-16-z", "ts": iso(C - 60), "hypothesis": "h",
             "lever": ["core/settle.py"], "commit": None, "prior": [],
             "blocked_by_core": "the v3 settle"},
            opened("E-2026-11-17-a", self.sha), closed("E-2026-11-17-a", C + DAY)])
        results = experiments.grade(self.repo.path, now=C + 5 * DAY)
        self.assertEqual([(r["id"], r["verdict"]) for r in results],
                         [("E-2026-11-16-z", "BLOCKED")])

    def test_cli_exits_1_on_a_refused_experiment(self):
        self.repo.flush()
        self.repo.ledger([opened("E-2026-11-17-a", self.sha, baseline=9.0)])
        out = io.StringIO()
        with mock.patch.object(experiments.grade, "__defaults__", (self.repo.path, C + DAY)), \
                contextlib.redirect_stdout(out):
            code = experiments.main(["grade"])
        self.assertEqual(code, 1)
        self.assertIn("E-2026-11-17-a REFUSED", out.getvalue())


class TestRowsWindow(Base):
    """abs_net_delta over a rows window: the last N rows that settled before
    the commit, and the first N that began after it and have settled."""

    def alloc(self, entry: int, settled: int, realized: float, forecast: float = 0.05) -> None:
        self.repo.allocations.append({
            "id": f"a{len(self.repo.allocations):05d}", "ts": iso(entry), "kind": "morpho_market",
            "entry_ts": entry, "settled_ts": iso(settled), "status": "settled",
            "size_usdg": "250.000000", "realized_net_apy": realized,
            "forecast_net_apy": forecast})

    def test_baseline_and_forward_rows(self):
        self.alloc(C - 9 * DAY, C - 8 * DAY, 0.15)        # outside the last 2
        self.alloc(C - 6 * DAY, C - 5 * DAY, 0.07)        # |0.02|
        self.alloc(C - 3 * DAY, C - 2 * DAY, 0.09)        # |0.04|
        self.alloc(C - DAY, C + DAY, 0.50)                # began before: never counts
        self.alloc(C + DAY, C + 2 * DAY, 0.06)            # |0.01|
        self.repo.flush()
        sha = self.repo.experiment()
        got = experiments.propose("abs_net_delta", "rows=2", self.repo.path, sha, now=C + DAY)
        self.assertAlmostEqual(got["baseline"], 0.03)
        row = opened("E-2026-11-17-a", sha, metric="abs_net_delta", baseline=got["baseline"],
                     window={"rows": 2}, keep_if="< 0.02", rollback_if="> 0.05")
        self.repo.ledger([row])
        r = experiments.grade(self.repo.path, now=C + 3 * DAY)[0]
        self.assertEqual((r["verdict"], r.get("progress"), r.get("n")),
                         ("COLLECTING", "1/2 rows", 1), r)
        self.alloc(C + 2 * DAY, C + 3 * DAY, 0.08)        # |0.03|
        self.repo.flush()
        r = experiments.grade(self.repo.path, now=C + 4 * DAY)[0]
        self.assertEqual(r["verdict"], "KEEP")
        self.assertAlmostEqual(r["value"], 0.02)


class TestCatalog(Base):

    def test_agent_vs_best_mae_pairs_with_the_best_baseline(self):
        for day in range(3):
            self.repo.forecasts.append({
                "id": f"f{day:06d}", "forecaster": "agent", "kind": "morpho_market",
                "horizon_min": 1440, "status": "settled", "entry_ts": C - (day + 2) * DAY,
                "settled_ts": iso(C - (day + 1) * DAY),
                "errors": {"agent": 0.01, "advertised": 0.03, "trailing_24h": 0.02,
                           "trailing_7d": None, "haircut": None}})
        self.repo.flush()
        got = experiments.propose("agent_vs_best_mae:morpho_market", "days=7",
                                  self.repo.path, now=C)
        self.assertAlmostEqual(got["baseline"], -0.01)
        with self.assertRaisesRegex(experiments.ExperimentError, "is not one of lending"):
            experiments.propose("agent_vs_best_mae:uniswap_v3_pool", "days=7",
                                self.repo.path, now=C)

    def graded_row(self, entry: int, settled: int, **kw) -> dict:
        r = {"id": f"f{len(self.repo.forecasts):06d}", "forecaster": "agent",
             "kind": "morpho_market", "ts": iso(entry), "entry_ts": entry,
             "status": "settled", "settled_ts": iso(settled)}
        r.update(kw)
        return r

    def test_graded_rows_per_day_counts_rows_that_began_and_settled_in_the_window(self):
        row = self.graded_row
        self.repo.forecasts += [
            row(C - 3 * DAY, C - DAY), row(C - 2 * DAY, C - DAY),    # counted: 3 in 4 days
            row(C - 4 * DAY, C - 3 * DAY),
            row(C - 3 * DAY, C - DAY, status="void"),                # a void is not graded
            row(C - 3 * DAY, C - DAY, forecaster="trailing_24h"),    # nor a baseline
            row(C - 9 * DAY, C - DAY),                               # began before the window
            row(C - 6 * DAY, C - 5 * DAY),                           # settled before it
        ]
        self.repo.flush()
        got = experiments.propose("graded_rows_per_day", "days=4", self.repo.path, now=C)
        self.assertEqual((got["baseline"], got["n"]), (0.75, 3))

    def test_graded_rows_per_day_after_the_commit_skips_rows_recorded_before_it(self):
        row = self.graded_row
        self.repo.forecasts += [row(C - 3 * DAY, C - DAY), row(C - 2 * DAY, C - DAY)]
        sha = self.repo.experiment()
        self.repo.flush()
        baseline = experiments.propose("graded_rows_per_day", "days=4", self.repo.path,
                                       sha, now=C)["baseline"]
        self.assertEqual(baseline, 0.5)
        self.repo.forecasts += [
            row(C + 3600, C + DAY), row(C + 7200, C + 2 * DAY),      # counted: 2 in 4 days
            row(C - DAY, C + DAY), row(C - 3600, C + DAY),           # recorded before the commit
            row(C + 3 * DAY, C + 5 * DAY),                           # settles after the window
        ]
        self.repo.flush()
        self.repo.ledger([opened("E-2026-11-17-a", sha, metric="graded_rows_per_day",
                                 baseline=baseline, keep_if="> 1", rollback_if="< 0.25")])
        r = experiments.grade(self.repo.path, now=C + 5 * DAY)[0]
        self.assertEqual((r["verdict"], r["value"], r["n"]), ("INCONCLUSIVE", 0.5, 2), r)

    def test_full_cycle_seconds_reads_full_hours_only(self):
        for ts, wall in ((C - DAY + 3600, 300.0), (C - DAY + 3700, 200.0),     # 01Z: 500
                         (C - DAY + 13 * 3600, 700.0),                       # 13Z: 700
                         (C - DAY + 5 * 3600, 999.0)):                       # light tick
            self.repo.usage.append({"ts": iso(ts), "wall_seconds": wall})
        self.repo.flush()
        sha = self.repo.experiment()
        got = experiments.propose("full_cycle_seconds", "days=2", self.repo.path, sha, now=C)
        self.assertEqual((got["baseline"], got["n"]), (600.0, 2))

    def test_replay_pass_has_no_value_on_too_few_rows(self):
        sha = self.repo.experiment(files={"strategy/forecast_rule.py": RULE + "# v2\n"})
        self.repo.flush()
        got = experiments.propose("replay_pass:strategy/forecast_rule.py", "days=7",
                                  self.repo.path, sha, now=C)
        self.assertIsNone(got["baseline"])
        self.assertIn("replay FAIL", got["detail"])


class TestStatus(Base):

    def test_status_folds_and_filters_by_lever(self):
        a, b = self.repo.experiment(C), self.repo.experiment(C + 60)
        self.repo.ledger([opened("E-2026-11-17-a", a, C + 30), closed("E-2026-11-17-a", C + 50),
                          opened("E-2026-11-17-b", b, C + 90, lever=["strategy/risk.json"])])
        data = experiments.status(self.repo.path)
        self.assertEqual(data["counts"], {"open": 1, "blocked": 0, "closed": 1})
        hist = experiments.status(self.repo.path, lever="strategy/playbook.md")
        self.assertEqual([e["id"] for e in hist["experiments"]], ["E-2026-11-17-a"])
        self.assertEqual(hist["experiments"][0]["outcome"], "kept")


# --------------------------------------------------------------------------
# The append-only guard in CI
# --------------------------------------------------------------------------

class TestBoundaryAppendOnly(Base):

    def boundary(self, before: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(BOUNDARY), before, "HEAD"], cwd=self.repo.path,
                              capture_output=True, text=True)

    def test_appending_passes_and_rewriting_fails(self):
        start = self.repo.commit({experiments.LEDGER_REL: '{"a": 1}\n'}, "deep-retro: open", C)
        self.repo.commit({experiments.LEDGER_REL: '{"a": 1}\n{"b": 2}\n'}, "deep-retro: add",
                         C + 60)
        self.assertEqual(self.boundary(start).returncode, 0)
        mid = self.repo.git("rev-parse", "HEAD")
        self.repo.commit({experiments.LEDGER_REL: '{"a": 9}\n{"b": 2}\n'}, "deep-retro: edit",
                         C + 120)
        r = self.boundary(mid)
        self.assertEqual(r.returncode, 1)
        self.assertIn("append-only", r.stdout)

    def test_an_operator_commit_may_rewrite_it(self):
        start = self.repo.commit({experiments.LEDGER_REL: '{"a": 1}\n'}, "deep-retro: open", C)
        self.repo.commit({experiments.LEDGER_REL: '{"a": 2}\n'}, "operator: repair", C + 60)
        self.assertEqual(self.boundary(start).returncode, 0)


if __name__ == "__main__":
    unittest.main()
