"""The replay gate, on synthetic settled rows and a throwaway git repository.

PROTECTED ENGINE FILE. The allocating agent may not edit this file.

The test that matters most is `TestForecastTimeOnly`: if a rule could see
anything written at or after settlement, the gate would approve hindsight.
Nothing here touches the network.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zlib

from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import forecast, replay, rpc

DAY_S = 86_400
START = 1_790_000_000 - 1_790_000_000 % DAY_S
MORPHO = "morpho_market"
T24 = "def forecast(s):\n    return s['trailing_24h']\n"


def setUpModule() -> None:
    rpc.forbid_network("the replay reads the journal and nothing else")


def row(day: int, realized: float, t24: float | None = 0.05, t7: float | None = 0.05,
        advertised: float | None = 0.05, kind: str = MORPHO, status: str = "settled") -> dict:
    """A settled row as the ledger holds it, outcome fields included."""
    rid = f"f{row.count:06d}"
    row.count += 1
    return {"id": rid, "ts": "2026-09-24T00:00:00Z", "forecaster": "baseline",
            "venue": "0x" + "ab" * 20, "market_id": "0x" + "cd" * 32, "kind": kind,
            "horizon_min": 1440, "size_usdg": "250.000000", "cycle": "7", "note": "a note",
            "entry_block": 100 + day, "entry_ts": START + day * DAY_S + 3600,
            "entry_depth_usdg": "1000.000000", "entry_state": {"market_fee": "0"},
            "forecast_gross_apy": None, "status": status,
            "baselines": {"advertised": advertised, "trailing_24h": t24,
                          "trailing_7d": t7, "haircut": None},
            "baseline_notes": {}, "exit_block": 200 + day,
            "exit_ts": START + (day + 1) * DAY_S, "exit_state": {"market_fee": "1"},
            "hold_minutes": 1440.0, "realized_gross_usdg": "0.1",
            "realized_gross_apy": realized, "errors": {"advertised": realized - 0.05},
            "settle_method": "m", "settle_detail": {"price_ratio": "1"},
            "settled_ts": "2026-09-25T00:00:00Z"}


row.count = 0


def board(days: int = 80, jitter: float = 0.01) -> list[dict]:
    """Rows on many (kind, day) clusters, realized moving around the baseline."""
    return [row(day, 0.05 + jitter * ((day + i) % 3 - 1), t24=0.05 + jitter * (i - 1) / 2)
            for day in range(days) for i in range(3)]


class TestForecastTimeOnly(unittest.TestCase):
    OUTCOME_FIELDS = forecast.REALIZED_FIELDS + ("forecast_gross_apy", "forecaster", "note",
                                                 "cycle", "status", "id", "ts", "baseline_notes")

    def test_a_snapshot_is_the_allowlist_and_holds_no_outcome_field(self):
        got = replay.snapshot(row(0, 9.99))
        self.assertEqual(tuple(sorted(got)), tuple(sorted(replay.SNAPSHOT_FIELDS)))
        self.assertEqual(set(got) & set(self.OUTCOME_FIELDS), set())
        self.assertEqual(set(got["entry_state"]), {"market_fee"})

    def test_a_field_the_ledger_gains_later_stays_out(self):
        got = replay.snapshot(dict(row(0, 0.1), exit_something_new=1))
        self.assertNotIn("exit_something_new", got)

    def test_no_outcome_can_change_what_a_rule_forecasts(self):
        """A rule whose number is a checksum of everything it was handed. If
        any outcome reached the snapshot, changing the outcomes would move a
        number here. This is the test that fails if the gate leaks hindsight."""
        def peek(s: dict) -> float:
            return zlib.crc32(json.dumps(s, sort_keys=True).encode()) / 2**32

        rows = board(days=4)
        before = replay.predict(peek, replay.scored_rows(rows))
        moved = copy.deepcopy(rows)
        for r in moved:
            r.update(realized_gross_apy=7.0, errors={"advertised": 6.95}, exit_block=1,
                     exit_ts=2, exit_state={"share_price": "9"}, hold_minutes=3.0,
                     realized_gross_usdg="9", settle_method="x",
                     settle_detail={"price_ratio": "9"}, settled_ts="2030-01-01T00:00:00Z",
                     forecast_gross_apy=0.99, forecaster="agent", note="it paid 700%",
                     cycle="999", baseline_notes={"x": 1})
        self.assertEqual(replay.predict(peek, replay.scored_rows(moved)), before)

    def test_the_entry_state_does_move_the_forecast(self):
        """The checksum above is not blind: what entry knew does reach it."""
        def peek(s: dict) -> float:
            return zlib.crc32(json.dumps(s, sort_keys=True).encode()) / 2**32

        a = row(0, 0.1)
        b = copy.deepcopy(a)
        b["entry_state"]["market_fee"] = "5"
        self.assertNotEqual(replay.predict(peek, [a]), replay.predict(peek, [b]))

    def test_a_rule_is_called_oldest_entry_first_whatever_the_file_order(self):
        seen = []

        def rule(s: dict) -> None:
            seen.append(s["entry_ts"])

        rows = [row(3, 0.1), row(1, 0.1), row(2, 0.1)]
        replay.predict(rule, replay.scored_rows(rows))
        self.assertEqual(seen, sorted(seen))

    def test_a_rule_cannot_change_a_row(self):
        def vandal(s: dict) -> float:
            s["entry_state"]["market_fee"] = "tampered"
            return 0.1

        r = row(0, 0.1)
        replay.predict(vandal, [r])
        self.assertEqual(r["entry_state"], {"market_fee": "0"})


class TestScoredRows(unittest.TestCase):
    def test_only_settled_lending_rows_score_oldest_first(self):
        rows = [row(2, 0.1), row(1, 0.1, kind="erc4626_usdg_vault"),
                row(0, 90.0, kind="uniswap_v3_pool"), row(0, 0.1, status="open"),
                row(0, 0.1, status="void")]
        self.assertEqual([r["id"] for r in replay.scored_rows(rows)],
                         [rows[1]["id"], rows[0]["id"]])

    def test_the_baseline_is_the_trailing_day_then_week_then_advert(self):
        rows = [row(0, 0.1, t24=0.1, t7=0.2, advertised=0.3),
                row(0, 0.1, t24=None, t7=0.2, advertised=0.3),
                row(0, 0.1, t24=None, t7=None, advertised=0.3),
                row(0, 0.1, t24=None, t7=None, advertised=None)]
        self.assertEqual(list(replay.baseline(rows).values()), [0.1, 0.2, 0.3, None])

    def test_the_halves_do_not_split_a_day(self):
        rows = replay.scored_rows([row(day, 0.1) for day in (0, 0, 1, 1, 1, 2, 3)])
        halves = replay.split(rows)
        self.assertEqual([r["entry_ts"] for r in halves["older"]], [rows[0]["entry_ts"]] * 2)
        self.assertEqual(len(halves["newer"]), 5)
        self.assertEqual(len(replay.split([row(0, 0.1), row(0, 0.1)])["older"]), 0)


class TestVerdict(unittest.TestCase):
    def setUp(self):
        self.rows = board()

    def near_outcome(self, offset: float):
        """A rule at the realized rate plus `offset`, fed from a list in call
        order, so a test can set how good a rule is. The snapshot cannot give
        it the outcome."""
        numbers = iter([float(r["realized_gross_apy"]) + offset
                        for r in replay.scored_rows(self.rows)])
        return lambda s: next(numbers)

    def test_the_baseline_itself_ties_and_passes(self):
        data = replay.replay(self.rows, lambda s: s["trailing_24h"])
        self.assertTrue(data["passed"])
        self.assertTrue(data["first_rule"])
        self.assertEqual({c["verdict"] for c in data["versus"]["t24h"].values()}, {"ties"})

    def test_a_rule_closer_than_the_baseline_beats_it(self):
        data = replay.replay(self.rows, self.near_outcome(0.0))
        self.assertEqual({c["verdict"] for c in data["versus"]["t24h"].values()}, {"beats"})
        self.assertTrue(data["passed"])

    def test_a_rule_worse_than_the_baseline_fails(self):
        data = replay.replay(self.rows, self.near_outcome(0.5))
        self.assertEqual({c["verdict"] for c in data["versus"]["t24h"].values()}, {"loses"})
        self.assertFalse(data["passed"])

    def test_losing_in_one_half_fails_even_when_the_mean_wins(self):
        """Far closer in the older half, slightly worse in the newer: fitted
        to one regime. The whole-ledger mean would approve this."""
        scored = replay.scored_rows(self.rows)
        halves = replay.split(scored)
        newer = {r["entry_ts"] for r in halves["newer"]}
        worse = iter(float(r["realized_gross_apy"]) + (0.03 if r["entry_ts"] in newer else 0.0)
                     for r in scored)
        data = replay.replay(self.rows, lambda s: next(worse))
        verdicts = {h: c["verdict"] for h, c in data["versus"]["t24h"].items()}
        self.assertEqual(verdicts, {"older": "beats", "newer": "loses"})
        self.assertFalse(data["passed"])

    def test_beating_the_baseline_but_losing_to_the_current_rule_fails(self):
        current = self.near_outcome(0.0)
        data = replay.replay(self.rows, self.near_outcome(0.005), current)
        self.assertEqual({c["verdict"] for c in data["versus"]["t24h"].values()}, {"beats"})
        self.assertEqual({c["verdict"] for c in data["versus"]["HEAD"].values()}, {"loses"})
        self.assertFalse(data["first_rule"])
        self.assertFalse(data["passed"])

    def test_abstaining_on_a_row_the_other_rule_spoke_on_fails(self):
        data = replay.replay(self.rows, lambda s: None if s["entry_block"] == 101
                             else s["trailing_24h"])
        self.assertEqual(data["versus"]["t24h"]["older"]["verdict"], "abstains")
        self.assertEqual(data["versus"]["t24h"]["older"]["abstained"], 3)
        self.assertFalse(data["passed"])

    def test_abstaining_where_nothing_spoke_is_no_loss(self):
        rows = self.rows + [row(5, 0.1, t24=None, t7=None, advertised=None)]
        self.assertTrue(replay.replay(rows, lambda s: s["trailing_24h"])["passed"])

    def test_too_few_rows_fail_rather_than_pass(self):
        data = replay.replay(board(days=4), lambda s: s["trailing_24h"] + 0.001)
        self.assertEqual({c["verdict"] for c in data["versus"]["t24h"].values()}, {"too-few"})
        self.assertFalse(data["passed"])

    def test_an_empty_ledger_fails_rather_than_passes(self):
        data = replay.replay([], lambda s: 0.1)
        self.assertEqual({c["verdict"] for c in data["versus"]["t24h"].values()}, {"no-rows"})
        self.assertFalse(data["passed"])

    def test_the_replay_line_names_the_verdict_and_every_comparison(self):
        line = replay.replay_line(replay.replay(self.rows, lambda s: s["trailing_24h"],
                                                lambda s: s["trailing_24h"]))
        self.assertTrue(line.startswith(f"replay PASS, {len(self.rows)} rows: vs t24h older "))
        self.assertIn("; vs HEAD older ", line)

    def test_a_rule_that_returns_junk_is_an_error_not_a_verdict(self):
        for junk in (float("nan"), float("inf"), True, "0.05"):
            with self.assertRaises(replay.ReplayError):
                replay.replay(self.rows, lambda s, value=junk: value)

    def test_a_rule_that_raises_is_an_error_naming_the_row(self):
        with self.assertRaisesRegex(replay.ReplayError, r"failed on f\d+"):
            replay.replay(self.rows, lambda s: 1 / 0)


class TestCommand(unittest.TestCase):
    """The command itself: working-tree rule against a git ref in a real
    repository, over a ledger on disk."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "strategy").mkdir()
        self.rule = self.root / "strategy" / "forecast_rule.py"
        ledger = self.root / "forecasts.jsonl"
        ledger.write_text("".join(json.dumps(r) + "\n" for r in board()))
        for name, value in (("ROOT", self.root), ("RULE_PATH", self.rule)):
            patcher = mock.patch.object(replay, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(forecast.load, "__defaults__", (ledger,))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git("init", "-q")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com",
                 "commit", "-q", "--allow-empty", "-m", "start")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def commit_rule(self, source: str) -> None:
        self.rule.write_text(source)
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com",
                 "commit", "-q", "-m", "rule")

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = replay.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_no_rule_file_is_an_error(self):
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("does not exist yet", err)

    def test_a_first_rule_is_judged_against_the_baseline_alone(self):
        self.rule.write_text(T24)
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("no earlier rule", out)
        self.assertNotIn("vs HEAD", out)

    def test_an_edit_worse_than_the_committed_rule_exits_one(self):
        self.commit_rule(T24)
        self.rule.write_text("def forecast(s):\n    return s['trailing_24h'] + 0.5\n")
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("replay FAIL", out)
        self.assertIn("vs HEAD", out)

    def test_ref_compares_a_committed_edit_with_its_parent(self):
        self.commit_rule(T24)
        self.commit_rule("def forecast(s):\n    return s['trailing_24h'] + 0.5\n")
        self.assertEqual(self.run_main()[0], 1)  # fails on the baseline alone
        code, out, _ = self.run_main("--ref", "HEAD~1")
        self.assertEqual(code, 1)
        self.assertIn("vs HEAD~1", out)

    def test_a_bad_ref_and_a_broken_rule_are_errors(self):
        self.rule.write_text(T24)
        self.assertEqual(self.run_main("--ref", "no-such-ref")[0], 2)
        self.rule.write_text("def forecast(s:\n")
        self.assertEqual(self.run_main()[0], 2)
        self.rule.write_text("x = 1\n")
        self.assertEqual(self.run_main()[0], 2)

    def test_the_command_forbids_the_network(self):
        self.rule.write_text("from core import rpc\n\n\ndef forecast(s):\n"
                             "    return rpc.call('eth_blockNumber')\n")
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("forbidden", err)


if __name__ == "__main__":
    unittest.main()
