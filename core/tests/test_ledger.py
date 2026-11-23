"""The caps refuse what they should, and a settled row's arithmetic holds.

PROTECTED ENGINE FILE. The allocating agent may not edit this file.

Everything here is offline and synthetic: a frozen quote and a frozen round
trip stand in for the chain, so the test measures the ledger's rules and not
the endpoint's mood.
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

from core import ledger, score, validate

CFG = {
    "quote_asset_decimals": 6,
    "sim_bankroll_usdg": 1000.0,
    "max_allocation_usdg": 250.0,
    "max_open_allocations": 12,
    "max_venue_share": 0.5,
    "max_new_allocations_per_cycle": 4,
    "min_hold_minutes": 60,
    "min_expected_net_bps": 5.0,
    "min_free_liquidity_multiple": 2.0,
    "real_execution_enabled": False,
}

# A Morpho market quote and a round trip, both frozen from what core/venues.py
# and core/cost.py returned on chain 4663 at block 58,084,708.
QUOTE = {
    "venue": "0x9d53d5e3bd5e8d4cbfa6db1ca238aea02e651010",
    "market_id": "0x" + "ab" * 32,
    "kind": "morpho_market", "block": 58_084_708, "ts": 1_777_900_000,
    "advertised_apy": 0.6346, "depth_usdg": "412000.000000",
    "rate_source": "borrowRateView at block 58084708",
    "settle_state": {"total_supply_assets": "412000000000",
                     "total_borrow_assets": "206000000000",
                     "total_supply_shares": "412000000000000000"},
}
TRIP = {"entry_cost_usdg": "0.100000", "exit_cost_usdg": "0.065100",
        "total_cost_usdg": "0.165100", "gas_price_gwei": 0.1002}
IDLE = {"venue": "idle", "kind": "idle", "block": 58_084_708, "ts": 1_777_900_000,
        "advertised_apy": 0.0, "depth_usdg": None, "settle_state": {},
        "rate_source": "capital held in USDG earns nothing on 4663"}
IDLE_TRIP = {"entry_cost_usdg": "0.000000", "exit_cost_usdg": "0.000000",
             "total_cost_usdg": "0.000000", "gas_price_gwei": 0.1002}

DAY = 1440


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "allocations.jsonl"
        self.addCleanup(self.dir.cleanup)

    def row(self, size="250", horizon=7 * DAY, gross=0.6346, cycle="c1",
            quote=None, trip=None):
        return ledger.build_row(quote or QUOTE, size, horizon, gross,
                                trip or TRIP, cycle=cycle)

    def add(self, **kwargs):
        return ledger.append(self.row(**kwargs), self.path, CFG)


class TestForecast(LedgerCase):
    def test_forecast_nets_the_round_trip_out_of_the_advertised_rate(self):
        row = self.add()
        # 250 USDG for seven days at 63.46% earns 2.366 USDG gross and pays
        # 0.1651 to get in and out, so the net forecast is 88 basis points.
        self.assertEqual(row["forecast_net_usdg"], "2.202055")
        self.assertAlmostEqual(row["forecast_net_bps"], 88.08, places=2)
        self.assertAlmostEqual(row["forecast_net_apy"], 0.57976, places=4)
        self.assertEqual(row["advertised_apy"], 0.6346)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["entry_state"], QUOTE["settle_state"])

    def test_amounts_are_decimal_strings(self):
        row = self.add()
        for field in ledger.AMOUNT_FIELDS:
            if row.get(field) is not None:
                self.assertIsInstance(row[field], str, field)
                self.assertRegex(row[field], ledger.AMOUNT_RE, field)

    def test_an_open_row_carries_no_realized_field(self):
        row = self.add()
        for field in ledger.REALIZED_FIELDS:
            self.assertIsNone(row.get(field), field)

    def test_a_forecast_is_not_the_advertised_rate_after_costs(self):
        row = self.add()
        self.assertLess(row["forecast_net_apy"], row["advertised_apy"])


class TestCaps(LedgerCase):
    def refuses(self, needle, **kwargs):
        before = ledger.load(self.path)
        with self.assertRaises(ledger.LedgerError) as caught:
            self.add(**kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(ledger.load(self.path), before,
                         "a refused write reached the file")

    def test_over_the_allocation_cap(self):
        self.refuses("max_allocation_usdg", size="250.000001")

    def test_over_the_bankroll(self):
        for n in range(4):
            self.add(cycle=f"c{n}", quote=dict(QUOTE, venue=f"0x{n:040x}"))
        self.refuses("sim_bankroll_usdg", cycle="c9",
                     quote=dict(QUOTE, venue="0x" + "9" * 40))

    def test_over_the_venue_share(self):
        self.add(cycle="c1")
        self.add(cycle="c2")
        with self.assertRaises(ledger.LedgerError) as caught:
            self.add(cycle="c3")
        self.assertIn("max_venue_share", str(caught.exception))

    def test_over_the_per_cycle_count(self):
        other = dict(QUOTE, venue="0x" + "cd" * 20, market_id=None)
        for n in range(4):
            ledger.append(self.row(size="100", quote=dict(other, market_id=f"0x{n:064x}")),
                          self.path, CFG)
        with self.assertRaises(ledger.LedgerError) as caught:
            ledger.append(self.row(size="100", quote=other), self.path, CFG)
        self.assertIn("max_new_allocations_per_cycle", str(caught.exception))

    def test_under_the_hold_floor(self):
        self.refuses("min_hold_minutes", horizon=30)

    def test_under_the_net_forecast_floor(self):
        # One day at 5.04% earns 0.034 USDG on 250 and the round trip costs
        # 0.165, so this allocation loses money and the ledger will not take it.
        self.refuses("min_expected_net_bps", horizon=DAY, gross=0.0504)

    def test_an_hour_at_the_best_rate_on_the_chain_is_still_refused(self):
        # min_hold_minutes permits a one-hour hold; on this chain no rate pays
        # for a round trip in an hour, so the cost floor refuses it anyway.
        self.refuses("min_expected_net_bps", horizon=60, gross=0.6346)

    def test_a_market_that_cannot_pay_the_position_back_is_refused(self):
        # a00001 on 2026-09-09: 99.95% utilization, 179.7 USDG free on 360k.
        pinned = dict(QUOTE, settle_state={"total_supply_assets": "360078645410",
                                           "total_borrow_assets": "359898933110",
                                           "total_supply_shares": "355050669831541663"})
        self.refuses("a withdraw would revert", quote=pinned)

    def test_free_liquidity_that_cannot_be_verified_is_refused(self):
        blind = dict(QUOTE, settle_state={"total_supply_assets": "412000000000",
                                          "total_supply_shares": "412000000000000000"})
        self.refuses("cannot be verified", quote=blind)

    def test_a_vault_with_too_little_idle_usdg_is_refused(self):
        drained = dict(QUOTE, kind="erc4626_usdg_vault", market_id=None,
                       settle_state={"share_price": "1000000", "share_unit": "1000000000000000000",
                                     "total_assets": "77143000000", "total_shares": "77000000000000000000000",
                                     "idle_assets": "499999999"})
        self.refuses("a withdraw would revert", quote=drained)

    def test_a_vault_that_can_pay_out_is_accepted(self):
        liquid = dict(QUOTE, kind="erc4626_usdg_vault", market_id=None,
                      settle_state={"share_price": "1000000", "share_unit": "1000000000000000000",
                                    "total_assets": "77143000000", "total_shares": "77000000000000000000000",
                                    "idle_assets": "9826000000"})
        self.add(quote=liquid)
        self.assertEqual(ledger.load(self.path)[0]["kind"], "erc4626_usdg_vault")

    def test_a_wrapper_vault_whose_simulated_withdraw_executes_is_accepted(self):
        tranche = dict(QUOTE, kind="erc4626_usdg_vault", market_id=None,
                       settle_state={"share_price": "1000000", "total_assets": "88619118751",
                                     "idle_assets": "0",
                                     "exit_simulation": {"ok": True, "assets": "500000000",
                                                         "holder": "0xabc"}})
        self.add(quote=tranche)
        self.assertEqual(len(ledger.load(self.path)), 1)

    def test_a_vault_whose_simulated_withdraw_reverts_is_refused_whatever_it_holds(self):
        stuck = dict(QUOTE, kind="erc4626_usdg_vault", market_id=None,
                     settle_state={"share_price": "1000000", "total_assets": "88619118751",
                                   "idle_assets": "9826000000",
                                   "exit_simulation": {"ok": False, "assets": "500000000",
                                                       "holder": None,
                                                       "errors": ["0x1234567890: {'code': 3}"]}})
        self.refuses("simulated withdraw of 500.000000 USDG reverted", quote=stuck)

    def test_a_vault_whose_simulation_is_inconclusive_is_refused_whatever_it_holds(self):
        """No holder with the shares to ask proves nothing either way, so the
        entry is refused as unverifiable; its idle USDG does not stand in."""
        unknown = dict(QUOTE, kind="erc4626_usdg_vault", market_id=None,
                       settle_state={"share_price": "1000000", "total_assets": "88619118751",
                                     "idle_assets": "9826000000",
                                     "exit_simulation": {"ok": None, "assets": "500000000",
                                                         "holder": None, "errors": [],
                                                         "note": "inconclusive: no recent "
                                                                 "depositor found"}})
        self.refuses("exit simulation was inconclusive", quote=unknown)

    def test_a_vault_with_no_share_price_is_refused(self):
        blind = dict(QUOTE, kind="erc4626_usdg_vault", market_id=None,
                     settle_state={"share_price": "None", "total_assets": "71311830000",
                                   "exit_simulation": {"ok": True, "assets": "500000000", "holder": "0xabc"}})
        self.refuses("no share_price", quote=blind)

    def test_a_vault_without_the_idle_field_is_refused(self):
        blind = dict(QUOTE, kind="erc4626_usdg_vault", market_id=None,
                     settle_state={"share_price": "1000000", "total_assets": "77143000000"})
        self.refuses("cannot be verified", quote=blind)

    def test_idle_is_exempt_from_the_allocation_cap_and_the_floor(self):
        row = ledger.append(self.row(size="750", horizon=DAY, gross=0.0,
                                     quote=IDLE, trip=IDLE_TRIP), self.path, CFG)
        self.assertEqual(row["kind"], "idle")
        self.assertEqual(row["forecast_net_usdg"], "0.000000")
        self.assertEqual(row["forecast_net_bps"], 0.0)

    def test_idle_still_consumes_bankroll(self):
        ledger.append(self.row(size="800", horizon=DAY, gross=0.0,
                               quote=IDLE, trip=IDLE_TRIP), self.path, CFG)
        with self.assertRaises(ledger.LedgerError) as caught:
            self.add(size="250")
        self.assertIn("sim_bankroll_usdg", str(caught.exception))


class TestSettle(LedgerCase):
    def test_a_winner_settles_to_what_it_actually_made(self):
        row = self.add()
        exit_ts = row["entry_ts"] + 7 * 86_400
        out = ledger.settle(row["id"], exit_block=58_084_708 + 6_000_000,
                            exit_ts=exit_ts, exit_cost_usdg="0.065100",
                            realized_gross_usdg="2.101000",
                            exit_state={"total_supply_assets": "413000000000"},
                            path=self.path, cfg=CFG)
        self.assertEqual(out["status"], "settled")
        self.assertEqual(out["realized_usdg"], "1.935900")   # 2.101 - 0.1651
        self.assertAlmostEqual(out["realized_net_apy"], 0.49514, places=4)
        self.assertLess(out["realized_net_apy"], out["advertised_apy"])
        self.assertEqual(out["hold_minutes"], 10080.0)

    def test_gas_larger_than_the_yield_settles_as_a_loss(self):
        row = self.add(horizon=DAY, gross=1.5)
        exit_ts = row["entry_ts"] + 86_400
        out = ledger.settle(row["id"], exit_block=58_084_708 + 857_000,
                            exit_ts=exit_ts, exit_cost_usdg="0.065100",
                            realized_gross_usdg="0.010000", path=self.path, cfg=CFG)
        self.assertEqual(out["realized_usdg"], "-0.155100")
        self.assertAlmostEqual(out["realized_net_apy"], -0.20269, places=4)
        self.assertLess(out["realized_net_apy"], out["forecast_net_apy"])

    def test_settlement_before_the_hold_floor_is_refused(self):
        row = self.add()
        with self.assertRaises(ledger.LedgerError) as caught:
            ledger.settle(row["id"], exit_block=1, exit_ts=row["entry_ts"] + 1800,
                          exit_cost_usdg="0.065100", realized_gross_usdg="0.5",
                          path=self.path, cfg=CFG)
        self.assertIn("min_hold_minutes", str(caught.exception))
        self.assertEqual(ledger.load(self.path)[0]["status"], "open")

    def test_a_settled_row_cannot_settle_twice(self):
        row = self.add()
        ledger.settle(row["id"], exit_block=1, exit_ts=row["entry_ts"] + 7 * 86_400,
                      exit_cost_usdg="0.065100", realized_gross_usdg="2.1",
                      path=self.path, cfg=CFG)
        with self.assertRaises(ledger.LedgerError):
            ledger.settle(row["id"], exit_block=1, exit_ts=row["entry_ts"] + 8 * 86_400,
                          exit_cost_usdg="0.065100", realized_gross_usdg="9.9",
                          path=self.path, cfg=CFG)

    def test_void_needs_a_reason(self):
        row = self.add()
        with self.assertRaises(ledger.LedgerError):
            ledger.void(row["id"], "", path=self.path)
        out = ledger.void(row["id"], "the market stopped answering", path=self.path)
        self.assertEqual(out["status"], "void")
        self.assertIsNone(out.get("realized_net_apy"))


class TestValidate(LedgerCase):
    """The tripwires catch a ledger edited by hand."""

    def settled_book(self):
        row = self.add()
        ledger.settle(row["id"], exit_block=64_000_000,
                      exit_ts=row["entry_ts"] + 7 * 86_400,
                      exit_cost_usdg="0.065100", realized_gross_usdg="2.101000",
                      path=self.path, cfg=CFG)
        idle = ledger.append(self.row(size="750", horizon=DAY, gross=0.0,
                                      quote=IDLE, trip=IDLE_TRIP), self.path, CFG)
        ledger.settle(idle["id"], exit_block=64_000_000,
                      exit_ts=idle["entry_ts"] + 86_400, exit_cost_usdg="0.000000",
                      realized_gross_usdg="0.000000", path=self.path, cfg=CFG)
        return ledger.load(self.path)

    def failures(self, rows):
        ledger.save(rows, self.path)
        rep = validate.Report()
        validate.check_ledger(rep, CFG, self.path)
        return rep.failures

    def test_a_clean_book_passes(self):
        self.assertEqual(self.failures(self.settled_book()), [])

    def test_a_realized_field_on_an_open_row_fails(self):
        rows = self.settled_book()
        rows[0]["status"] = "open"
        self.assertTrue(any("realized" in f for f in self.failures(rows)))

    def test_a_float_amount_fails(self):
        rows = self.settled_book()
        rows[0]["size_usdg"] = 250.0
        self.assertTrue(any("decimal string" in f for f in self.failures(rows)))

    def test_a_size_over_the_cap_fails(self):
        rows = self.settled_book()
        rows[0]["size_usdg"] = "400.000000"
        self.assertTrue(any("max_allocation_usdg" in f for f in self.failures(rows)))

    def test_realized_that_does_not_follow_from_the_amounts_fails(self):
        rows = self.settled_book()
        rows[0]["realized_net_apy"] = 0.99
        self.assertTrue(any("does not follow" in f for f in self.failures(rows)))

    def test_costs_quietly_dropped_from_the_realized_total_fails(self):
        rows = self.settled_book()
        rows[0]["realized_usdg"] = rows[0]["realized_gross_usdg"]
        self.assertTrue(any("Costs must be charged" in f for f in self.failures(rows)))

    def test_the_advertised_rate_copied_into_the_realized_field_fails(self):
        rows = self.settled_book()
        rows[0]["realized_net_apy"] = rows[0]["advertised_apy"]
        self.assertTrue(any("advertised" in f for f in self.failures(rows)))

    def test_a_cap_break_between_rows_written_in_the_same_second_fails(self):
        # The replay walks the file in order rather than by timestamp: three
        # rows written inside one second still have to see each other, or a
        # burst of allocations could break the venue share unnoticed.
        self.add(cycle="c1")
        self.add(cycle="c2")
        rows = [dict(r, ts="2026-09-08T23:00:00Z") for r in ledger.load(self.path)]
        rows.append(dict(rows[-1], id="a00003", cycle="c3"))
        self.assertTrue(any("max_venue_share" in f for f in self.failures(rows)))

    def test_a_corrupted_morpho_market_id_fails(self):
        rep = validate.Report()
        validate.check_venues(rep, {"quote_asset_address":
                                    "0x0000000000000000000000000000000000000001"})
        self.assertTrue(any("reproduce" in f for f in rep.failures))

    def test_the_real_leg_must_stay_off(self):
        rep = validate.Report()
        validate.check_config(rep)
        self.assertEqual(rep.failures, [])
        rep = validate.Report()
        rep.check(False, "sanity: Report records a failure")
        self.assertEqual(len(rep.failures), 1)


class TestScore(LedgerCase):
    def test_the_three_deltas(self):
        row = self.add()
        ledger.settle(row["id"], exit_block=64_000_000,
                      exit_ts=row["entry_ts"] + 7 * 86_400,
                      exit_cost_usdg="0.065100", realized_gross_usdg="2.101000",
                      path=self.path, cfg=CFG)
        rows = ledger.load(self.path)
        data = score.report(rows)
        settled = rows[0]
        overall = data["overall"]
        self.assertEqual(overall["n"], 1)
        self.assertAlmostEqual(
            overall["net_delta"],
            settled["realized_net_apy"] - settled["forecast_net_apy"])
        self.assertAlmostEqual(
            overall["advertised_delta"],
            settled["realized_net_apy"] - settled["advertised_apy"])
        self.assertAlmostEqual(overall["idle_delta"], settled["realized_net_apy"])
        self.assertEqual(data["idle_baseline_apy"], 0.0)
        self.assertEqual(overall["realized_usdg"], "1.935900")
        self.assertEqual(overall["cost_usdg"], "0.165100")

    def test_the_idle_baseline_comes_from_settled_idle_rows(self):
        idle = ledger.append(self.row(size="750", horizon=DAY, gross=0.0,
                                      quote=IDLE, trip=IDLE_TRIP), self.path, CFG)
        ledger.settle(idle["id"], exit_block=64_000_000,
                      exit_ts=idle["entry_ts"] + 86_400, exit_cost_usdg="0.000000",
                      realized_gross_usdg="0.000000", path=self.path, cfg=CFG)
        data = score.report(ledger.load(self.path))
        self.assertEqual(data["idle_baseline_apy"], 0.0)
        self.assertIn("settled idle rows", data["idle_baseline_source"])
        self.assertEqual(data["by_kind"]["idle"]["realized_net_apy"], 0.0)
        self.assertEqual(data["deployed"]["n"], 0)

    def test_the_report_is_json(self):
        self.add()
        json.dumps(score.report(ledger.load(self.path)))


if __name__ == "__main__":
    unittest.main()
