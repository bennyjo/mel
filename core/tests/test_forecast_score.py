"""The forecast score and the forecast tripwires.

PROTECTED ENGINE FILE. The allocating agent may not edit this file.

The clustered statistic is checked against numbers worked by hand. The report
and the tripwires run on rows `core/forecast.py` wrote and settled from the
recorded Morpho state `test_forecast.py` replays, and on synthetic settled
rows where a test needs many clusters or a chosen error pattern. Nothing here
touches the network.
"""

from __future__ import annotations

import math
import statistics
import sys
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import forecast, rpc, score, validate
from core.tests.test_forecast import (DAY, ENTRY_TS, EXIT_BLOCK, MORPHO, MORPHO_QUOTE, Case,
                                      exit_quote, morpho_trailing, quoter)

DAY_S = 86_400


def setUpModule() -> None:
    rpc.forbid_network("the forecast score reads the journal and nothing else")


def srow(forecaster: str, kind: str, day: int, realized: float, preds: dict,
         horizon: int = DAY, rid: int = 0) -> dict:
    """A settled row reduced to what the score reads. `preds` maps each
    predictor to its number or None; errors are realized minus predicted."""
    full = {p: preds.get(p) for p in forecast.PREDICTORS}
    return {"id": f"f{rid:06d}", "forecaster": forecaster, "kind": kind,
            "venue": "0x" + "ab" * 20, "horizon_min": horizon,
            "entry_ts": ENTRY_TS + day * DAY_S + 3600, "status": "settled",
            "forecast_gross_apy": full["agent"], "realized_gross_apy": realized,
            "errors": {p: None if v is None else realized - v for p, v in full.items()}}


# --------------------------------------------------------------------------
# The clustered statistic
# --------------------------------------------------------------------------

class TestClusteredMean(unittest.TestCase):
    def test_identical_rows_in_a_cluster_are_worth_one_row(self):
        """Three clusters of four identical rows: twelve rows, three
        observations. Worked by hand: residual cluster sums -4, 0, 4, so the
        CR1 variance of the mean is 3/2 x 32 / 12^2 and t has 2 degrees of
        freedom."""
        values = [1.0] * 4 + [2.0] * 4 + [3.0] * 4
        clusters = ["a"] * 4 + ["b"] * 4 + ["c"] * 4
        got = score.clustered_mean(values, clusters)
        self.assertEqual((got["n"], got["clusters"]), (12, 3))
        self.assertAlmostEqual(got["mean"], 2.0)
        self.assertAlmostEqual(got["n_eff"], 3.0)
        se = math.sqrt(1.5 * 32) / 12
        self.assertAlmostEqual(got["se"], se)
        self.assertAlmostEqual(got["lo"], 2.0 - 4.303 * se)
        self.assertAlmostEqual(got["hi"], 2.0 + 4.303 * se)

    def test_rows_in_clusters_of_one_are_independent(self):
        """With every row its own cluster the CR1 error is the ordinary
        standard error of the mean and the effective n is n."""
        values = [0.03, -0.01, 0.02, 0.05, -0.04, 0.00, 0.01]
        got = score.clustered_mean(values, [str(i) for i in range(len(values))])
        self.assertAlmostEqual(got["n_eff"], 7.0)
        self.assertAlmostEqual(got["se"], statistics.stdev(values) / math.sqrt(7))
        self.assertAlmostEqual(got["hi"] - got["mean"], 2.447 * got["se"])

    def test_one_cluster_has_no_interval(self):
        got = score.clustered_mean([0.1, 0.2, 0.3], ["x"] * 3)
        self.assertIsNone(got["lo"])
        self.assertEqual(got["clusters"], 1)
        self.assertEqual(score.clustered_mean([], [])["n"], 0)

    def test_an_astronomical_value_gives_inf_instead_of_raising(self):
        """`x ** 2` raises OverflowError on a float past about 1.3e154, where
        `x * x` gives inf. A settled row can carry such a realized APY, and
        the score must still run."""
        got = score.clustered_mean([0.1, 1.4e236, 0.2], ["a", "b", "c"])
        self.assertEqual(got["n"], 3)
        self.assertEqual(got["mean"], statistics.fmean([0.1, 1.4e236, 0.2]))

    def test_the_t_table_rounds_toward_the_wider_interval(self):
        self.assertEqual(score.t975(35), score.T975[30])
        self.assertEqual(score.t975(10_000), 1.980)

    def test_a_cluster_is_a_kind_on_a_utc_day(self):
        a = srow("agent", "morpho_market", 0, 0.1, {"agent": 0.1})
        b = dict(a, horizon_min=10080, entry_ts=a["entry_ts"] + 3600)
        c = dict(a, kind="erc4626_usdg_vault")
        self.assertEqual(score.cluster_of(a), score.cluster_of(b))
        self.assertNotEqual(score.cluster_of(a), score.cluster_of(c))


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

class TestForecastReport(unittest.TestCase):
    def test_recent_windows_score_by_entry_age_and_leave_out_pools(self):
        rows = [srow("agent", "morpho_market", day, 0.05, {"agent": 0.05, "advertised": 0.06})
                for day in (5, 12, 20, 25, 30, 40)]
        rows.append(srow("agent", "uniswap_v3_pool", 40, 90.0, {"agent": 0.05, "advertised": 0.06}))
        now = ENTRY_TS + 41 * DAY_S
        block = score.forecast_report(rows, now)["by_forecaster"]["agent"]["by_window"]
        self.assertEqual({w: g["n"] for w, g in block.items()},
                         {"0-7 d": 1, "7-14 d": 1, "14-28 d": 2})
        self.assertAlmostEqual(block["0-7 d"]["versus"]["advertised"]["diff"]["mean"], -0.01)

    def board(self, days: int, agent_error: float) -> list[dict]:
        """Baseline rows on which trailing_7d is best, and agent rows on which
        advertised happens to be best, so the choice of pool shows."""
        rows = []
        for day in range(days):
            for kind in ("morpho_market", "erc4626_usdg_vault"):
                realized = 0.05 + 0.001 * day
                rows.append(srow("baseline", kind, day, realized,
                                 {"advertised": realized + 0.04, "trailing_24h": realized - 0.02,
                                  "trailing_7d": realized + 0.005, "haircut": None}))
                rows.append(srow("agent", kind, day, realized,
                                 {"agent": realized + agent_error + 0.001 * (day % 3),
                                  "advertised": realized, "trailing_24h": realized - 0.02,
                                  "trailing_7d": realized + 0.005, "haircut": realized + 0.01}))
        return rows

    def test_the_best_baseline_is_ranked_on_baseline_rows_not_the_agents(self):
        data = score.forecast_report(self.board(3, 0.001))
        best = data["best_baseline"]["morpho_market:1440"]
        self.assertEqual(best["rank"][0], "trailing_7d")
        self.assertIn("baseline rows of the kind", best["source"])
        # haircut is null on every baseline row, so it does not rank there.
        self.assertNotIn("haircut", best["rank"])
        versus = data["by_forecaster"]["agent"]["all"]["versus"]
        self.assertEqual(versus["best"]["used"], {"trailing_7d": 6})

    def test_with_no_baseline_row_the_ranking_is_in_sample_and_says_so(self):
        rows = [r for r in self.board(3, 0.001) if r["forecaster"] == "agent"]
        best = score.forecast_report(rows)["best_baseline"]["morpho_market:1440"]
        self.assertEqual(best["rank"][0], "advertised")
        self.assertIn("in-sample", best["source"])

    def test_a_null_best_baseline_falls_to_the_next_in_rank(self):
        rows = self.board(2, 0.001)
        rows[1]["errors"]["trailing_7d"] = None
        versus = score.forecast_report(rows)["by_forecaster"]["agent"]["all"]["versus"]
        # Ranked on the baseline rows: trailing_7d, then trailing_24h, then advertised.
        self.assertEqual(versus["best"]["used"], {"trailing_7d": 3, "trailing_24h": 1})

    def test_mae_and_win_rate_follow_from_the_rows(self):
        data = score.forecast_report(self.board(3, 0.001))
        g = data["by_forecaster"]["agent"]["by_kind"]["morpho_market"]
        agent_rows = [r for r in self.board(3, 0.001)
                      if r["forecaster"] == "agent" and r["kind"] == "morpho_market"]
        self.assertAlmostEqual(g["mae"]["agent"]["mean"],
                               statistics.fmean(abs(r["errors"]["agent"]) for r in agent_rows))
        self.assertAlmostEqual(g["mae"]["advertised"]["mean"], 0.0)
        # The agent misses by 0.1 to 0.3 points; trailing_7d by 0.5.
        self.assertEqual(g["versus"]["trailing_7d"]["win_rate"]["mean"], 1.0)
        self.assertEqual(g["versus"]["advertised"]["win_rate"]["mean"], 0.0)
        self.assertLess(g["versus"]["best"]["diff"]["mean"], 0)
        self.assertEqual(g["clusters"], 3)
        self.assertEqual(g["bias"]["advertised"]["mean"], 0.0)

    def test_a_small_sample_says_it_is_too_small(self):
        data = score.forecast_report(self.board(3, 0.001))
        verdict = data["by_forecaster"]["agent"]["all"]["versus"]["best"]["verdict"]
        self.assertIn("too few to say", verdict)
        self.assertIn("6 (kind, day) clusters", verdict)

    def test_enough_clusters_give_a_verdict_either_way(self):
        better = score.forecast_report(self.board(40, 0.001))["by_forecaster"]["agent"]["all"]
        self.assertGreaterEqual(better["versus"]["best"]["diff"]["n_eff"], score.MIN_EFFECTIVE_N)
        self.assertEqual(better["versus"]["best"]["verdict"],
                         "the agent's error is smaller than the best baseline's")
        worse = score.forecast_report(self.board(40, 0.02))["by_forecaster"]["agent"]["all"]
        self.assertIn("a mechanical rule forecasts these rates better",
                      worse["versus"]["best"]["verdict"])

    def test_baseline_rows_are_scored_without_an_agent_comparison(self):
        g = score.forecast_report(self.board(2, 0.0))["by_forecaster"]["baseline"]["all"]
        self.assertNotIn("versus", g)
        self.assertNotIn("agent", g["mae"])
        self.assertNotIn("haircut", g["mae"])
        self.assertAlmostEqual(g["mae"]["trailing_7d"]["mean"], 0.005)

    def test_the_text_report_renders(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            score.print_forecasts(score.forecast_report(self.board(3, 0.001)))
            score.print_forecasts(score.forecast_report([]))
        text = buf.getvalue()
        self.assertIn("the agent against the best baseline", text)
        self.assertIn("nothing has settled yet", text)


class TestReportOnSettledRows(Case):
    """A baseline row and an agent row recorded from the replayed Morpho quote
    and settled by `core/forecast.py`, then scored."""

    def settled(self) -> list[dict]:
        self.baselines(MORPHO_QUOTE, morpho_trailing())
        self.agent(MORPHO_QUOTE, [DAY], [0.30], note="IRM at target; expect decay")
        exit_ = exit_quote(MORPHO_QUOTE, DAY + 1, MORPHO["exit_state"], EXIT_BLOCK)
        forecast.settle_due(self.path, now_ts=ENTRY_TS + (DAY + 2) * 60,
                            quoter=quoter(exit_), rebaseline=False)
        return self.rows()

    def test_both_row_types_score_from_their_own_errors(self):
        rows = self.settled()
        data = score.forecast_report(rows)
        self.assertEqual(data["by_status"], {"open": 2, "settled": 2, "void": 0})
        agent_row = next(r for r in rows if r["forecaster"] == "agent")
        base_row = next(r for r in rows if r["forecaster"] == "baseline" and r["status"] == "settled")
        agent = data["by_forecaster"]["agent"]["all"]
        self.assertAlmostEqual(agent["mae"]["agent"]["mean"], abs(agent_row["errors"]["agent"]))
        self.assertAlmostEqual(data["by_forecaster"]["baseline"]["all"]["mae"]["trailing_7d"]["mean"],
                               abs(base_row["errors"]["trailing_7d"]))
        best = data["best_baseline"]["morpho_market:1440"]
        want = min((abs(base_row["errors"][b]), b) for b in forecast.BASELINES
                   if base_row["errors"][b] is not None)[1]
        self.assertEqual(best["rank"][0], want)
        self.assertIn("too few to say", agent["versus"]["best"]["verdict"])
        self.assertAlmostEqual(agent["versus"]["best"]["diff"]["mean"],
                               abs(agent_row["errors"]["agent"]) - abs(agent_row["errors"][want]))


# --------------------------------------------------------------------------
# The tripwires
# --------------------------------------------------------------------------

class TestValidate(Case):
    def check(self) -> list[str]:
        rep = validate.Report()
        validate.check_forecasts(rep, self.path)
        return rep.failures

    def test_a_recorded_and_settled_ledger_passes(self):
        self.baselines(MORPHO_QUOTE, morpho_trailing())
        self.agent(MORPHO_QUOTE, [DAY], [0.30])
        exit_ = exit_quote(MORPHO_QUOTE, DAY + 1, MORPHO["exit_state"], EXIT_BLOCK)
        out = forecast.settle_due(self.path, now_ts=ENTRY_TS + (DAY + 2) * 60,
                                  quoter=quoter(exit_))
        self.assertTrue(out["baselines"]["recorded"])
        self.assertEqual(self.check(), [])

    def test_two_live_rows_on_one_key_fail(self):
        self.agent(MORPHO_QUOTE, [DAY], [0.30])
        rows = self.rows()
        rows.append(dict(rows[0], id="f000002"))
        forecast.save(rows, self.path)
        self.assertTrue(any("both live" in f for f in self.check()))

    def test_a_row_written_while_its_key_was_live_fails(self):
        """The earlier row closed after the later one was written: the later
        one superseded a live forecast, even though the file now shows one
        open row."""
        self.agent(MORPHO_QUOTE, [DAY], [0.30])
        rows = self.rows()
        rows[0].update(status="void", void_reason="replay",
                       settled_ts=forecast._now_iso(ENTRY_TS + 3 * DAY_S))
        rows.append(dict(rows[0], id="f000002", status="open", ts=forecast._now_iso(ENTRY_TS + 60)))
        for field in ("void_reason", "settled_ts"):
            rows[1].pop(field)
        forecast.save(rows, self.path)
        self.assertTrue(any("never superseded" in f for f in self.check()))

    def test_realized_fields_on_an_open_row_fail(self):
        self.agent(MORPHO_QUOTE, [DAY], [0.30])
        rows = self.rows()
        rows[0]["realized_gross_apy"] = 0.2
        forecast.save(rows, self.path)
        self.assertTrue(any("open but carries realized_gross_apy" in f for f in self.check()))

    def test_an_agent_forecast_written_after_its_entry_fails(self):
        self.agent(MORPHO_QUOTE, [DAY], [0.30])
        rows = self.rows()
        rows[0]["ts"] = forecast._now_iso(ENTRY_TS + 2 * 3600)
        forecast.save(rows, self.path)
        self.assertTrue(any("after its entry block" in f for f in self.check()))

    def test_a_malformed_row_fails_and_ids_must_rise(self):
        self.agent(MORPHO_QUOTE, [DAY, 4320], [0.30, 0.25])
        rows = self.rows()
        rows.reverse()
        rows.append({"id": "f000009"})
        forecast.save(rows, self.path)
        failures = self.check()
        self.assertTrue(any("follows a higher id" in f for f in failures))
        self.assertTrue(any("f000009 is missing" in f for f in failures))

    def test_no_file_is_a_note_not_a_failure(self):
        rep = validate.Report()
        validate.check_forecasts(rep, self.path)
        self.assertEqual(rep.failures, [])
        self.assertTrue(any("does not exist yet" in n for n in rep.notes))


if __name__ == "__main__":
    unittest.main()
