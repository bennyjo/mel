"""The forecast ledger, replayed against recorded on-chain state.

PROTECTED ENGINE FILE. The allocating agent may not edit this file.

Two recordings. `settle-state.json` is the Morpho market and v3 pool read at
blocks 58,093,251 and 58,099,049 that `test_settle.py` replays; where a test
here needs a hold longer than the 586 seconds between them, it moves the exit
timestamp, exactly as `test_settle.py` does, and the realized number is then
a replay of the recorded borrow rate over a longer clock rather than a claim
about what the market paid. `forecast-state.json` holds the trailing anchors
`core/forecast.py` chose from the chain's own events for that Morpho market,
and two live vault quotes from 2026-09-24: Steakhouse USDG, whose simulated
withdraw executed, and ArcadiaV2 Global Dollar Debt, whose simulated withdraw
reverted for every recent depositor.

Nothing here touches the network: `rpc.forbid_network` is on for the module,
so a path that quietly reads the chain fails here rather than in CI.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import forecast, ledger, rpc, settle

FIXTURES = Path(__file__).parent / "fixtures"
SETTLE = json.loads((FIXTURES / "settle-state.json").read_text())
STATE = json.loads((FIXTURES / "forecast-state.json").read_text())

ENTRY_TS = SETTLE["entry_ts"]
ENTRY_BLOCK = SETTLE["entry_block"]
EXIT_BLOCK = SETTLE["exit_block"]
MORPHO = SETTLE["morpho"]
V3 = SETTLE["v3"]
DAY = 1440

MORPHO_QUOTE = {"venue": MORPHO["venue"], "market_id": MORPHO["market_id"],
                "kind": "morpho_market", "block": ENTRY_BLOCK, "ts": ENTRY_TS,
                "advertised_apy": MORPHO["advertised_apy"], "depth_usdg": MORPHO["depth_usdg"],
                "rate_source": "market(bytes32) + irm borrowRateView",
                "settle_state": MORPHO["entry_state"]}
V3_QUOTE = {"venue": V3["venue"], "kind": "uniswap_v3_pool", "block": ENTRY_BLOCK,
            "ts": ENTRY_TS, "advertised_apy": 0.2351, "depth_usdg": V3["depth_usdg"],
            "rate_source": "feeGrowthGlobal delta", "settle_state": V3["entry_state"]}
VAULT_OK = STATE["vault_ok"]["quote"]
VAULT_STUCK = STATE["vault_stuck"]["quote"]
VAULT_PRICED = STATE["vault_priced"]["quote"]
PRICED_TRAILING = {name: (rate, {"source": "recorded 2026-09-24"})
                   for name, rate in STATE["vault_priced"]["trailing"].items()}

HAIRCUT = {"morpho_market": 0.6, "erc4626_usdg_vault": 0.75, "uniswap_v3_pool": 0.4}


def setUpModule() -> None:
    rpc.forbid_network("the forecast test replays recorded state and reads nothing")


def morpho_trailing() -> dict:
    return {name: forecast.trailing_from_anchor(MORPHO_QUOTE, name, v["anchor"])
            for name, v in STATE["morpho_trailing"].items()}


def exit_quote(quote: dict, hold_min: int, state: dict | None = None,
               block: int | None = None) -> dict:
    """The venue read again at the end of a hold: the recorded exit state on
    a clock moved to `hold_min` after entry."""
    return dict(quote, block=block or int(quote["block"]) + 1,
                ts=int(quote["ts"]) + hold_min * 60,
                settle_state=state if state is not None else quote["settle_state"])


def quoter(*quotes: dict):
    """A stand-in for the live exit read, returning recorded quotes."""
    by_key = {forecast.venue_key(q): q for q in quotes}
    return lambda targets: (by_key, None)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "forecasts.jsonl"
        risk = Path(self.dir.name) / "risk.json"
        risk.write_text(json.dumps({"forecast_haircut": HAIRCUT}))
        self._risk = forecast.RISK_PATH
        forecast.RISK_PATH = risk
        self.addCleanup(setattr, forecast, "RISK_PATH", self._risk)

    def baselines(self, quote: dict, trailing: dict | None = None,
                  now: int | None = None) -> dict:
        key = forecast.venue_key(quote)
        return forecast.record_baselines([quote], self.path,
                                         trailing={key: trailing or {}},
                                         now_ts=now or int(quote["ts"]) + 30)

    def agent(self, quote: dict, horizons: list[int], rates: list[float],
              note: str = "replay", now: int | None = None) -> dict:
        return forecast.record_from_quote(quote, horizons, rates, note, path=self.path,
                                          trailing=morpho_trailing()
                                          if quote is MORPHO_QUOTE else {},
                                          now_ts=now or int(quote["ts"]) + 30)

    def rows(self) -> list[dict]:
        return forecast.load(self.path)


# --------------------------------------------------------------------------
# Trailing baselines from recorded anchors
# --------------------------------------------------------------------------

class TestTrailing(unittest.TestCase):
    def test_the_recorded_morpho_anchors_reproduce_their_rates(self):
        """The anchors were chosen live from Supply and Withdraw events; the
        rate is recomputed here from the anchor and the accrued entry price."""
        for name, v in STATE["morpho_trailing"].items():
            rate, note = forecast.trailing_from_anchor(MORPHO_QUOTE, name, v["anchor"])
            self.assertAlmostEqual(rate, v["rate"], places=12)
            self.assertEqual(note["from_block"], v["anchor"]["block"])

    def test_the_vault_anchor_prices_against_convert_to_assets(self):
        for name, v in STATE["vault_ok"]["trailing"].items():
            rate, _ = forecast.trailing_from_anchor(VAULT_OK, name, v["anchor"])
            self.assertAlmostEqual(rate, v["rate"], places=12)
        # Steakhouse USDG paid about 3.9% over both windows.
        self.assertAlmostEqual(STATE["vault_ok"]["trailing"]["trailing_7d"]["rate"], 0.0389, places=3)

    def test_an_anchor_outside_the_band_is_null_with_the_reason(self):
        anchor = dict(STATE["morpho_trailing"]["trailing_24h"]["anchor"],
                      ts=ENTRY_TS - 3 * 86400)
        rate, note = forecast.trailing_from_anchor(MORPHO_QUOTE, "trailing_24h", anchor)
        self.assertIsNone(rate)
        self.assertIn("72.0 h before entry", note["reason"])

    def test_a_v3_pool_has_no_trailing_rate_and_reads_nothing_to_say_so(self):
        out = forecast.trailing_baselines([V3_QUOTE])[forecast.venue_key(V3_QUOTE)]
        for name in forecast.TRAILING:
            self.assertIsNone(out[name][0])
            self.assertIn("no share price", out[name][1]["reason"])

    def test_a_trailing_read_that_fails_nulls_the_baseline_instead_of_failing(self):
        out = forecast.trailing_baselines([MORPHO_QUOTE])[forecast.venue_key(MORPHO_QUOTE)]
        for name in forecast.TRAILING:
            self.assertIsNone(out[name][0])
            self.assertIn("network use is forbidden", out[name][1]["reason"])


# --------------------------------------------------------------------------
# A baseline row, from record to settle
# --------------------------------------------------------------------------

class TestBaselineRow(Case):
    def test_a_quote_records_one_baseline_row_per_horizon(self):
        out = self.baselines(MORPHO_QUOTE, morpho_trailing())
        self.assertEqual(sorted(r["horizon_min"] for r in out["recorded"]), list(forecast.HORIZONS))
        row = out["recorded"][0]
        self.assertEqual(row["forecaster"], "baseline")
        self.assertIsNone(row["forecast_gross_apy"])
        self.assertEqual(row["size_usdg"], "250.000000")
        self.assertEqual(row["entry_state"], MORPHO["entry_state"])
        b = row["baselines"]
        self.assertEqual(b["advertised"], MORPHO["advertised_apy"])
        self.assertAlmostEqual(b["haircut"], MORPHO["advertised_apy"] * 0.6)
        self.assertAlmostEqual(b["trailing_24h"], 0.23579, places=4)
        self.assertAlmostEqual(b["trailing_7d"], 0.34896, places=4)
        self.assertEqual(row["baseline_notes"]["haircut"]["factor"], 0.6)
        self.assertEqual(forecast.row_problems(row), [])

    def test_a_baseline_row_settles_on_the_arithmetic_settle_py_uses(self):
        self.baselines(MORPHO_QUOTE, morpho_trailing())
        exit_ = exit_quote(MORPHO_QUOTE, DAY + 2, MORPHO["exit_state"], EXIT_BLOCK)
        now = ENTRY_TS + (DAY + 5) * 60
        out = forecast.settle_due(self.path, now_ts=now, quoter=quoter(exit_))

        settled = [r for r in out["results"] if r["action"] == "settled"]
        self.assertEqual(len(settled), 1, "only the one-day row is due after a day")
        row = forecast.find(self.rows(), settled[0]["id"])
        self.assertEqual(row["status"], "settled")
        self.assertEqual(row["hold_minutes"], DAY + 2)

        # The same numbers, derived independently from settle.py and ledger.py.
        position = {"id": "x", "kind": "morpho_market", "size_usdg": "250.000000",
                    "entry_ts": ENTRY_TS, "entry_state": MORPHO["entry_state"]}
        gross = settle.realized_gross(position, MORPHO["exit_state"], exit_["ts"])["gross_usdg"]
        want = ledger.net_apy("250.000000", gross, DAY + 2)
        self.assertEqual(row["realized_gross_usdg"], gross)
        self.assertEqual(row["realized_gross_apy"], want)
        self.assertAlmostEqual(want, 0.6305, places=3)

        e = row["errors"]
        self.assertIsNone(e["agent"])
        self.assertAlmostEqual(e["advertised"], want - MORPHO["advertised_apy"])
        self.assertAlmostEqual(e["trailing_24h"], want - row["baselines"]["trailing_24h"])
        self.assertAlmostEqual(e["haircut"], want - row["baselines"]["haircut"])
        # The IRM's spot rate was the better predictor here: the market had
        # paid about 24% over the day before, and the replay pays about 63%.
        self.assertLess(abs(e["advertised"]), abs(e["trailing_24h"]))
        self.assertEqual(forecast.row_problems(row), [])

    def test_the_exit_quote_starts_the_next_baseline_row_on_the_same_block(self):
        self.baselines(MORPHO_QUOTE, morpho_trailing())
        exit_ = exit_quote(MORPHO_QUOTE, DAY + 2, MORPHO["exit_state"], EXIT_BLOCK)
        out = forecast.settle_due(self.path, now_ts=ENTRY_TS + (DAY + 5) * 60,
                                  quoter=quoter(exit_))
        fresh = out["baselines"]["recorded"]
        self.assertEqual([r["horizon_min"] for r in fresh], [DAY])
        self.assertEqual(fresh[0]["entry_block"], EXIT_BLOCK)
        self.assertEqual(fresh[0]["entry_state"], MORPHO["exit_state"])
        # Offline, the trailing read fails and says so rather than guessing.
        self.assertIsNone(fresh[0]["baselines"]["trailing_24h"])
        self.assertEqual(len([r for r in self.rows() if r["status"] == "open"]), 3)

    def test_a_row_is_not_settled_before_its_horizon(self):
        self.baselines(MORPHO_QUOTE, morpho_trailing())
        out = forecast.settle_due(self.path, now_ts=ENTRY_TS + (DAY - 1) * 60,
                                  quoter=quoter(MORPHO_QUOTE))
        self.assertEqual(out["results"], [])
        row = self.rows()[0]
        action, reason = forecast.settle_against(
            row, exit_quote(MORPHO_QUOTE, DAY - 1), ENTRY_TS + (DAY + 1) * 60)
        self.assertEqual((action, reason), ("retry", "the exit block is before the horizon"))

    def test_a_v3_row_settles_with_impermanent_loss_in_the_rate(self):
        self.baselines(V3_QUOTE)
        row = [r for r in self.rows() if r["horizon_min"] == DAY][0]
        self.assertIsNone(row["baselines"]["trailing_7d"])
        action, fields = forecast.settle_against(
            row, exit_quote(V3_QUOTE, DAY + 1, V3["exit_state"], EXIT_BLOCK),
            ENTRY_TS + (DAY + 2) * 60)
        self.assertEqual(action, "settled")
        # The price moved against the position over the recorded window and the
        # fees did not cover it, so the realized gross is negative.
        self.assertLess(fields["realized_gross_apy"], 0)
        self.assertIsNone(fields["errors"]["trailing_24h"])


# --------------------------------------------------------------------------
# An agent row
# --------------------------------------------------------------------------

class TestAgentRow(Case):
    def test_an_agent_row_carries_its_forecast_and_every_baseline(self):
        out = self.agent(MORPHO_QUOTE, [DAY, 4320, 10080], [0.30, 0.28, 0.25],
                         note="IRM at 63% but the market paid 24% last day")
        self.assertEqual(len(out["recorded"]), 3)
        row = out["recorded"][2]
        self.assertEqual(row["forecaster"], "agent")
        self.assertEqual(row["forecast_gross_apy"], 0.25)
        self.assertEqual(row["note"], "IRM at 63% but the market paid 24% last day")
        self.assertAlmostEqual(row["baselines"]["trailing_7d"], 0.34896, places=4)

    def test_an_agent_row_settles_and_is_scored_against_every_predictor(self):
        self.agent(MORPHO_QUOTE, [10080], [0.60])
        exit_ = exit_quote(MORPHO_QUOTE, 7 * DAY + 3, MORPHO["exit_state"], EXIT_BLOCK)
        out = forecast.settle_due(self.path, now_ts=ENTRY_TS + (7 * DAY + 5) * 60,
                                  quoter=quoter(exit_), rebaseline=False)
        self.assertEqual([r["action"] for r in out["results"]], ["settled"])
        row = self.rows()[0]
        realized = row["realized_gross_apy"]
        self.assertAlmostEqual(row["errors"]["agent"], realized - 0.60)
        self.assertAlmostEqual(row["errors"]["trailing_7d"],
                               realized - row["baselines"]["trailing_7d"])
        self.assertEqual(forecast.row_problems(row), [])

    def test_a_forecast_needs_a_note_a_known_horizon_and_a_sane_rate(self):
        with self.assertRaises(forecast.ForecastError):
            self.agent(MORPHO_QUOTE, [DAY], [0.3], note=" ")
        with self.assertRaises(forecast.ForecastError):
            self.agent(MORPHO_QUOTE, [720], [0.3])
        with self.assertRaises(forecast.ForecastError):
            self.agent(MORPHO_QUOTE, [DAY], [30.0])
        with self.assertRaises(forecast.ForecastError):
            self.agent(MORPHO_QUOTE, [DAY, 4320], [0.3])
        self.assertEqual(self.rows(), [])


# --------------------------------------------------------------------------
# One live row per forecaster, venue and horizon
# --------------------------------------------------------------------------

class TestUnitsGuard(Case):
    """f000187: `4.9` meaning 4.9% stored as 490% against 4.98% advertised."""

    def test_a_percentage_typed_as_a_number_is_refused(self):
        adv = MORPHO_QUOTE["advertised_apy"]
        with self.assertRaises(forecast.ForecastError) as caught:
            self.agent(MORPHO_QUOTE, [DAY], [adv * 10])
        self.assertIn("--confirm-extreme", str(caught.exception))
        self.assertEqual(self.rows(), [])

    def test_a_real_belief_above_the_line_passes_with_the_flag(self):
        adv = MORPHO_QUOTE["advertised_apy"]
        forecast.record_from_quote(MORPHO_QUOTE, [DAY], [adv * 4], "spike holds",
                                   path=self.path, trailing=morpho_trailing(),
                                   now_ts=int(MORPHO_QUOTE["ts"]) + 30,
                                   confirm_extreme=True)
        self.assertEqual(len(self.rows()), 1)

    def test_an_ordinary_forecast_above_advertised_passes(self):
        adv = MORPHO_QUOTE["advertised_apy"]
        self.agent(MORPHO_QUOTE, [DAY], [adv * 1.5])
        self.assertEqual(len(self.rows()), 1)

    def test_the_operator_void_keeps_the_record_and_takes_it_out(self):
        self.agent(MORPHO_QUOTE, [DAY], [0.04])
        rid = self.rows()[0]["id"]
        row = forecast.void_recording_error(rid, "units slip", self.path)
        self.assertEqual(row["status"], "void")
        self.assertIn("recording error (operator)", row["void_reason"])
        self.assertEqual(forecast.row_problems(row), []) if hasattr(forecast, "row_problems") else None
        with self.assertRaises(forecast.ForecastError):
            forecast.void_recording_error(rid, "again", self.path)


class TestOneLiveRow(Case):
    def test_a_second_forecast_on_a_live_key_is_refused_not_superseded(self):
        first = self.agent(MORPHO_QUOTE, [DAY], [0.30])["recorded"][0]
        again = self.agent(MORPHO_QUOTE, [DAY], [0.45], now=ENTRY_TS + 60)
        self.assertEqual(again["recorded"], [])
        self.assertIn(first["id"], again["refused"][0]["reason"])
        self.assertIn("never superseded", again["refused"][0]["reason"])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["forecast_gross_apy"], 0.30)

    def test_the_free_horizons_are_recorded_and_the_live_one_refused(self):
        self.agent(MORPHO_QUOTE, [DAY], [0.30])
        out = self.agent(MORPHO_QUOTE, [DAY, 4320], [0.30, 0.29])
        self.assertEqual([r["horizon_min"] for r in out["recorded"]], [4320])
        self.assertEqual([r["horizon_min"] for r in out["refused"]], [DAY])

    def test_baseline_and_agent_rows_are_separate_streams(self):
        self.baselines(MORPHO_QUOTE, morpho_trailing())
        out = self.agent(MORPHO_QUOTE, [DAY], [0.30])
        self.assertEqual(len(out["recorded"]), 1)
        again = self.baselines(MORPHO_QUOTE, morpho_trailing())
        self.assertEqual(again["recorded"], [])
        self.assertEqual(again["already_live"], 3)

    def test_a_settled_key_is_free_again(self):
        self.agent(MORPHO_QUOTE, [DAY], [0.30])
        exit_ = exit_quote(MORPHO_QUOTE, DAY + 1, MORPHO["exit_state"], EXIT_BLOCK)
        forecast.settle_due(self.path, now_ts=ENTRY_TS + (DAY + 2) * 60,
                            quoter=quoter(exit_), rebaseline=False)
        later = dict(exit_, ts=exit_["ts"] + 60)
        out = self.agent(later, [DAY], [0.50], now=later["ts"] + 30)
        self.assertEqual(len(out["recorded"]), 1)


# --------------------------------------------------------------------------
# Rows that cannot settle honestly
# --------------------------------------------------------------------------

class TestVoid(Case):
    def test_a_vault_that_cannot_pay_the_position_back_is_void_with_the_reason(self):
        """ArcadiaV2 Global Dollar Debt, as read on 2026-09-24: its views
        answer and its share price moves, but a withdraw of twice the
        position reverted for every one of its largest recent depositors.
        A rate measured from that share price describes an exit that could
        not have happened."""
        out = self.baselines(VAULT_STUCK)
        self.assertEqual(len(out["recorded"]), 3)
        exit_ = exit_quote(VAULT_STUCK, DAY + 1)
        now = int(VAULT_STUCK["ts"]) + (DAY + 2) * 60
        res = forecast.settle_due(self.path, now_ts=now, quoter=quoter(exit_),
                                  rebaseline=False)
        self.assertEqual([r["action"] for r in res["results"]], ["void"])
        row = [r for r in self.rows() if r["status"] == "void"][0]
        self.assertIn("not exitable at the horizon", row["void_reason"])
        self.assertIn("reverted for every holder tried", row["void_reason"])
        self.assertIn("0xced4f633", row["void_reason"])
        self.assertNotIn("realized_gross_apy", row)
        self.assertEqual(forecast.row_problems(row), [])

    def test_a_vault_that_can_pay_settles_from_its_share_price(self):
        self.baselines(VAULT_OK)
        moved = dict(VAULT_OK["settle_state"], share_price="1008047")
        action, fields = forecast.settle_against(
            self.rows()[0], exit_quote(VAULT_OK, DAY + 1, moved),
            int(VAULT_OK["ts"]) + (DAY + 2) * 60)
        self.assertEqual(action, "settled")
        # 108 units on 1,007,939 over a day: 3.98% compounded.
        self.assertAlmostEqual(fields["realized_gross_apy"], 0.0399, places=3)

    def test_a_vault_with_no_share_price_is_never_recorded(self):
        blind = dict(VAULT_OK, settle_state=dict(VAULT_OK["settle_state"], share_price="None"))
        out = self.baselines(blind)
        self.assertEqual(out["recorded"], [])
        self.assertIn("could never settle", out["skipped"][0]["reason"])
        with self.assertRaises(forecast.ForecastError):
            forecast.record_from_quote(blind, [DAY], [0.04], "x", path=self.path,
                                       trailing={}, now_ts=int(VAULT_OK["ts"]) + 30)

    def test_a_v3_pool_with_no_liquidity_is_never_recorded(self):
        # Settlement dilutes the fee credit by the pool's liquidity at entry,
        # so a row without it could never settle.
        state = {k: v for k, v in V3_QUOTE["settle_state"].items() if k != "liquidity"}
        out = self.baselines(dict(V3_QUOTE, settle_state=state))
        self.assertEqual(out["recorded"], [])
        self.assertIn("entry state has no liquidity", out["skipped"][0]["reason"])

    def test_a_kind_without_settlement_maths_is_skipped(self):
        pair = {"venue": "0x8803", "kind": "uniswap_v2_pool", "block": 1, "ts": 1,
                "settle_state": {"reserve0": "1"}}
        out = forecast.record_baselines([pair], self.path, trailing={})
        self.assertEqual(out["recorded"], [])
        self.assertIn("no settlement maths", out["skipped"][0]["reason"])

    def test_a_vault_priced_by_a_leveraged_asset_gets_no_row(self):
        # Arcus HOOD (3x Long) holds USDG but its share is leveraged HOOD: its
        # own last week reads +15 million percent, its last day -100%.
        out = self.baselines(VAULT_PRICED, PRICED_TRAILING)
        self.assertEqual(out["recorded"], [])
        reason = out["skipped"][0]["reason"]
        self.assertIn("advertised +1364.86%", reason)
        self.assertIn("moves with a price, not a rate", reason)
        with self.assertRaises(forecast.ForecastError):
            forecast.record_from_quote(VAULT_PRICED, [DAY], [0.10], "x", path=self.path,
                                       trailing=PRICED_TRAILING,
                                       now_ts=int(VAULT_PRICED["ts"]) + 30)
        self.assertEqual(self.rows(), [])

    def test_an_unreadable_exit_is_retried_then_void_after_the_grace(self):
        self.baselines(MORPHO_QUOTE, morpho_trailing())
        row = [r for r in self.rows() if r["horizon_min"] == DAY][0]
        broken = {"error": "market(bytes32) did not answer"}
        soon = ENTRY_TS + (DAY + 60) * 60
        self.assertEqual(forecast.settle_against(row, broken, soon)[0], "retry")
        late = ENTRY_TS + (DAY + forecast.SETTLE_GRACE_MIN + 1) * 60
        action, reason = forecast.settle_against(row, broken, late)
        self.assertEqual(action, "void")
        self.assertIn("did not answer", reason)

    def test_a_morpho_market_drained_at_exit_is_void(self):
        self.baselines(MORPHO_QUOTE, morpho_trailing())
        row = [r for r in self.rows() if r["horizon_min"] == DAY][0]
        supply = int(MORPHO["exit_state"]["total_supply_assets"])
        pinned = dict(MORPHO["exit_state"], total_borrow_assets=str(supply - 499_999_999))
        action, reason = forecast.settle_against(
            row, exit_quote(MORPHO_QUOTE, DAY + 1, pinned), ENTRY_TS + (DAY + 2) * 60)
        self.assertEqual(action, "void")
        self.assertIn("free liquidity at exit is 499.999999 USDG", reason)


# --------------------------------------------------------------------------
# The tripwires validate.py runs over every row
# --------------------------------------------------------------------------

class TestSettleBudget(Case):
    """Cycles 615 and 640 to 642: one `settle` over a due batch with several
    slow venues ran past the 600-second tool limit, was killed, and wrote
    nothing. Settle writes each venue as it finishes and starts no new one
    past its budget."""

    def setUp(self) -> None:
        super().setUp()
        self.baselines(MORPHO_QUOTE)
        self.baselines(V3_QUOTE)
        self.now = ENTRY_TS + (DAY + 5) * 60
        self.exits = quoter(exit_quote(MORPHO_QUOTE, DAY + 2, MORPHO["exit_state"], EXIT_BLOCK),
                            exit_quote(V3_QUOTE, DAY + 2, V3["exit_state"], EXIT_BLOCK))

    def open_due(self) -> list[str]:
        return [r["id"] for r in forecast.due(self.rows(), self.now)]

    def test_no_venue_starts_past_the_budget_and_the_rest_stay_due(self):
        ticks = iter([0, 300])       # the first venue read takes five minutes
        out = forecast.settle_due(self.path, now_ts=self.now, quoter=self.exits,
                                  rebaseline=False, budget_seconds=240,
                                  clock=lambda: next(ticks))
        self.assertEqual([r["action"] for r in out["results"]], ["settled"])
        self.assertEqual(out["deferred"], self.open_due())
        self.assertEqual(len(out["deferred"]), 1)

        again = forecast.settle_due(self.path, now_ts=self.now, quoter=self.exits,
                                    rebaseline=False, budget_seconds=240)
        self.assertEqual([r["action"] for r in again["results"]], ["settled"])
        self.assertEqual(again["deferred"], [])
        self.assertEqual(self.open_due(), [])

    def test_a_run_killed_partway_keeps_the_venues_it_finished(self):
        calls = []

        def dies_on_the_second_venue(targets):
            calls.append(targets)
            if len(calls) == 2:
                raise KeyboardInterrupt("the tool limit")
            return self.exits(targets)

        with self.assertRaises(KeyboardInterrupt):
            forecast.settle_due(self.path, now_ts=self.now, quoter=dies_on_the_second_venue,
                                rebaseline=False)
        settled = [r for r in self.rows() if r["status"] == "settled"]
        self.assertEqual([r["id"] for r in settled], [calls[0][0]["id"]])
        self.assertEqual(self.open_due(), [calls[1][0]["id"]])


class TestRowProblems(Case):
    def row(self) -> dict:
        return self.agent(MORPHO_QUOTE, [DAY], [0.30])["recorded"][0]

    def test_an_open_row_with_a_realized_field_is_flagged(self):
        row = dict(self.row(), realized_gross_apy=0.2)
        self.assertTrue(any("open but carries realized_gross_apy" in p
                            for p in forecast.row_problems(row)))

    def test_a_forecast_written_long_after_its_entry_is_flagged(self):
        row = dict(self.row(), ts=forecast._now_iso(ENTRY_TS + 3600))
        self.assertTrue(any("after its entry block" in p for p in forecast.row_problems(row)))

    def test_a_baseline_row_carrying_a_forecast_is_flagged(self):
        row = dict(self.row(), forecaster="baseline")
        self.assertTrue(any("baseline row carrying a forecast" in p
                            for p in forecast.row_problems(row)))

    def test_a_baseline_outside_the_forecast_domain_is_flagged(self):
        row = self.row()
        row["baselines"] = dict(row["baselines"], trailing_7d=1535.2)
        self.assertTrue(any("moves with a price" in p for p in forecast.row_problems(row)))

    def test_a_settled_error_that_does_not_follow_is_flagged(self):
        self.row()
        exit_ = exit_quote(MORPHO_QUOTE, DAY + 1, MORPHO["exit_state"], EXIT_BLOCK)
        forecast.settle_due(self.path, now_ts=ENTRY_TS + (DAY + 2) * 60,
                            quoter=quoter(exit_), rebaseline=False)
        row = self.rows()[0]
        self.assertEqual(forecast.row_problems(row), [])
        row["errors"] = dict(row["errors"], agent=0.0)
        self.assertTrue(any("errors.agent" in p for p in forecast.row_problems(row)))
        row = self.rows()[0]
        row["realized_gross_apy"] += 0.01
        self.assertTrue(any("does not follow" in p for p in forecast.row_problems(row)))


if __name__ == "__main__":
    unittest.main()
