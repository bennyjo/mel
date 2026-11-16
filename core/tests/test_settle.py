"""Settlement replayed against on-chain state recorded from two blocks.

PROTECTED ENGINE FILE. The allocating agent may not edit this file.

`core/tests/fixtures/settle-state.json` is real state read from chain 4663 at
blocks 58,093,251 and 58,099,049, 586 seconds apart, for the deepest Morpho
market that quotes a rate and for the 33M USDG Uniswap v3 pool. It was
recorded because it cannot be re-read: the public endpoint drops state after
about ten minutes, so a settlement test that reached for the chain would work
today and fail tomorrow.

Where a test needs a hold longer than the 586 seconds between the two recorded
blocks, it moves the exit timestamp and says so. That is a replay of the same
two states over a longer clock, not a claim about what the venue paid over
that period, and each such test says which of the two it is asserting. The
recorded gas snapshot is used the same way, so every cost here is what the
chain charged at block 58,099,049 and not a round number.

Nothing in this file touches the network. `rpc.forbid_network` is on for the
whole module, so a settlement path that quietly started reading the chain
would fail here rather than in CI.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import cost, ledger, rpc, score, settle, validate

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "settle-state.json").read_text())

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

ENTRY_TS = FIXTURE["entry_ts"]
EXIT_TS = FIXTURE["exit_ts"]
EXIT_BLOCK = FIXTURE["exit_block"]
HOUR, DAY = 60, 1440

MORPHO = FIXTURE["morpho"]
V3 = FIXTURE["v3"]
GAS = FIXTURE["gas"]

# The gas snapshot as `core/cost.py` would have built it at block 58,099,049,
# and the pool the swap legs price against, both from the recording.
SNAPSHOT = {"block": GAS["block"], "ts": EXIT_TS,
            "gas_price_wei": int(GAS["gas_price_gwei"] * 1e9),
            "gas_price_gwei": GAS["gas_price_gwei"], "eth_usdg": GAS["eth_usdg"],
            "usdg_per_gas": GAS["usdg_per_gas"]}
POOL = {"address": V3["venue"], "fee_ppm": V3["fee_ppm"], "usdg_is_token0": False,
        "liquidity": int(V3["exit_state"]["liquidity"]),
        "sqrt_price_x96": int(V3["exit_state"]["sqrt_price_x96"])}

MORPHO_QUOTE = {"venue": MORPHO["venue"], "market_id": MORPHO["market_id"],
                "kind": "morpho_market", "block": FIXTURE["entry_block"], "ts": ENTRY_TS,
                "advertised_apy": MORPHO["advertised_apy"],
                "depth_usdg": MORPHO["depth_usdg"],
                "rate_source": "market(bytes32) + irm borrowRateView",
                "settle_state": MORPHO["entry_state"]}
V3_QUOTE = {"venue": V3["venue"], "kind": "uniswap_v3_pool",
            "block": FIXTURE["entry_block"], "ts": ENTRY_TS,
            "advertised_apy": 0.2351, "depth_usdg": V3["depth_usdg"],
            "rate_source": "feeGrowthGlobal delta", "settle_state": V3["entry_state"]}
IDLE_QUOTE = {"venue": "idle", "kind": "idle", "block": FIXTURE["entry_block"],
              "ts": ENTRY_TS, "advertised_apy": 0.0, "depth_usdg": None,
              "rate_source": "capital held in USDG earns nothing on 4663",
              "settle_state": {}}


def setUpModule() -> None:
    rpc.forbid_network("the settlement test replays recorded state and reads nothing")


def trip(kind: str, size: float) -> dict:
    return cost.round_trip(kind, size, SNAPSHOT, POOL)


def open_row(kind: str, size: str, horizon_min: int) -> dict:
    """A synthetic open row, built by the ledger from the recorded quote so it
    carries the same entry state a live open would have carried."""
    quote = {"morpho_market": MORPHO_QUOTE, "uniswap_v3_pool": V3_QUOTE,
             "idle": IDLE_QUOTE}[kind]
    gross = {"morpho_market": MORPHO["advertised_apy"],
             "uniswap_v3_pool": 0.30, "idle": 0.0}[kind]
    return ledger.build_row(quote, size, horizon_min, gross, trip(kind, float(size)),
                            cycle="replay")


class ReplayCase(unittest.TestCase):
    """Open a row through the ledger, then settle it against recorded state."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "allocations.jsonl"
        self.addCleanup(self.dir.cleanup)

    def replay(self, kind: str, size: str, horizon_min: int, hold_min: int,
               exit_state: dict | None = None) -> tuple[dict, dict]:
        row = ledger.append(open_row(kind, size, horizon_min), self.path, CFG)
        state = exit_state if exit_state is not None else {
            "morpho_market": MORPHO["exit_state"], "uniswap_v3_pool": V3["exit_state"],
            "idle": {}}[kind]
        exit_ts = ENTRY_TS + hold_min * 60
        outcome = settle.realized_gross(row, state, exit_ts)
        settled = ledger.settle(row["id"], EXIT_BLOCK, exit_ts,
                                trip(kind, float(size))["exit_cost_usdg"],
                                outcome["gross_usdg"], exit_state=state,
                                path=self.path, cfg=CFG)
        return settled, outcome


# --------------------------------------------------------------------------
# Morpho
# --------------------------------------------------------------------------

class TestMorphoAccrual(unittest.TestCase):
    """The market was untouched across both recorded blocks: `last_update` is
    the same 1,788,910,452 in each. Settling off the stored state alone would
    report a supplier earned nothing over the hold, which is false - a real
    withdraw accrues first."""

    def test_the_two_recorded_states_are_identical(self):
        self.assertEqual(MORPHO["entry_state"]["total_supply_assets"],
                         MORPHO["exit_state"]["total_supply_assets"])
        self.assertEqual(MORPHO["entry_state"]["last_update"],
                         MORPHO["exit_state"]["last_update"])

    def test_stored_state_alone_would_report_a_flat_share_price(self):
        stored = {k: v for k, v in MORPHO["exit_state"].items() if k != "borrow_rate_wad"}
        row = {"id": "x", "kind": "morpho_market", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS,
               "entry_state": {k: v for k, v in MORPHO["entry_state"].items()
                               if k != "borrow_rate_wad"}}
        out = settle.realized_gross(row, stored, ENTRY_TS + 7 * 86400)
        self.assertEqual(out["gross_usdg"], "0.000000")
        self.assertFalse(out["detail"]["exit_accrued"])
        self.assertIn("no borrow rate", out["detail"]["note"])

    def test_accrual_moves_the_share_price_at_the_market_rate(self):
        row = {"id": "x", "kind": "morpho_market", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS, "entry_state": MORPHO["entry_state"]}
        out = settle.realized_gross(row, MORPHO["exit_state"], ENTRY_TS + 7 * 86400)
        self.assertEqual(out["gross_usdg"], "2.355052")
        self.assertTrue(out["detail"]["exit_accrued"])
        self.assertEqual(out["detail"]["exit_accrued_seconds"], 605493)

    def test_an_exit_the_market_cannot_pay_is_refused_and_the_row_stays_open(self):
        """Interest accrues to supply and borrow alike, so free liquidity is
        the raw gap. Drain it to under twice the position and the settlement
        must refuse rather than price an exit that would revert on chain."""
        row = {"id": "x", "kind": "morpho_market", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS, "entry_state": MORPHO["entry_state"]}
        supply = int(MORPHO["exit_state"]["total_supply_assets"])
        pinned = dict(MORPHO["exit_state"], total_borrow_assets=str(supply - 499_999_999))
        with self.assertRaises(settle.SettleError) as caught:
            settle.realized_gross(row, pinned, ENTRY_TS + 7 * 86400)
        self.assertIn("would revert", str(caught.exception))
        self.assertIn("stays open", str(caught.exception))

    def test_a_vault_exit_it_cannot_pay_is_refused(self):
        row = {"id": "v", "kind": "erc4626_usdg_vault", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS,
               "entry_state": {"share_price": "1000000", "idle_assets": "9826000000"}}
        drained = {"share_price": "1001000", "idle_assets": "100000000"}
        with self.assertRaises(settle.SettleError) as caught:
            settle.realized_gross(row, drained, ENTRY_TS + 7 * 86400)
        self.assertIn("would revert", str(caught.exception))

    def test_a_wrapper_vault_settles_on_a_simulated_withdraw_not_its_balance(self):
        row = {"id": "v", "kind": "erc4626_usdg_vault", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS, "entry_state": {"share_price": "1000000"}}
        exit_ = {"share_price": "1001000", "idle_assets": "0",
                 "exit_simulation": {"ok": True, "assets": "500000000", "holder": "0xabc"}}
        out = settle.realized_gross(row, exit_, ENTRY_TS + 7 * 86400)
        self.assertEqual(out["gross_usdg"], "0.250000")

    def test_a_vault_exit_it_can_pay_settles_from_the_share_price(self):
        row = {"id": "v", "kind": "erc4626_usdg_vault", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS,
               "entry_state": {"share_price": "1000000", "idle_assets": "9826000000"}}
        out = settle.realized_gross(row, {"share_price": "1001000", "idle_assets": "9826000000"},
                                    ENTRY_TS + 7 * 86400)
        self.assertEqual(out["gross_usdg"], "0.250000")

    def test_the_realized_rate_agrees_with_the_advertised_one(self):
        """A cross-check of two numbers that never touch each other in the
        code. `advertised_apy` came from the IRM's `borrowRateView` scaled by
        utilization; this comes from replaying Morpho's accrual into the share
        price. They land 0.36 percentage points apart, which is the three-term
        Taylor accrual against the exponential the quote uses."""
        row = {"id": "x", "kind": "morpho_market", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS, "entry_state": MORPHO["entry_state"]}
        out = settle.realized_gross(row, MORPHO["exit_state"], ENTRY_TS + 7 * 86400)
        gross_apy = ledger.net_apy("250.000000", out["gross_usdg"], 7 * DAY)
        self.assertAlmostEqual(gross_apy, 0.63052, places=4)
        self.assertLess(abs(gross_apy - MORPHO["advertised_apy"]), 0.005)


class TestMorphoSettles(ReplayCase):
    def test_a_seven_day_hold_settles_a_win_just_under_its_forecast(self):
        settled, _ = self.replay("morpho_market", "250", 7 * DAY, 7 * DAY)
        self.assertEqual(settled["status"], "settled")
        self.assertEqual(settled["realized_gross_usdg"], "2.355052")
        self.assertEqual(settled["exit_cost_usdg"], "0.068277")
        self.assertEqual(settled["entry_cost_usdg"], "0.095271")
        # 2.355052 gross less 0.163548 of round trip.
        self.assertEqual(settled["realized_usdg"], "2.191504")
        self.assertAlmostEqual(settled["realized_net_apy"], 0.57632, places=4)
        # The forecast used the advertised rate, so the miss is small and
        # negative: the round trip was priced right and the venue paid what it
        # said it would.
        delta = settled["realized_net_apy"] - settled["forecast_net_apy"]
        self.assertAlmostEqual(delta, -0.00346, places=4)

    def test_an_early_exit_after_two_hours_is_a_loss_to_gas(self):
        """The row forecast +88 bps over seven days and was legal to write.
        Closed after two hours it earned 0.027905 USDG and paid 0.163548 to get
        in and out, so it settles as a loss. This is the case the repo exists
        to make visible: nothing about the venue changed, only the hold."""
        settled, outcome = self.replay("morpho_market", "250", 7 * DAY, 2 * HOUR)
        self.assertEqual(outcome["gross_usdg"], "0.027905")
        self.assertEqual(settled["realized_usdg"], "-0.135643")
        self.assertLess(float(settled["realized_gross_usdg"]),
                        float(settled["entry_cost_usdg"]) + float(settled["exit_cost_usdg"]))
        self.assertAlmostEqual(settled["realized_net_apy"], -0.90718, places=4)
        self.assertGreater(settled["forecast_net_apy"], 0)

    def test_the_recorded_ten_minute_window_is_under_the_hold_floor(self):
        """The two blocks are 586 seconds apart. The arithmetic still works -
        0.002272 USDG of interest - but the ledger refuses to score a hold
        shorter than `min_hold_minutes`, so the window is only ever an
        arithmetic fixture and never a settled row."""
        row = ledger.append(open_row("morpho_market", "250", 7 * DAY), self.path, CFG)
        outcome = settle.realized_gross(row, MORPHO["exit_state"], EXIT_TS)
        self.assertEqual(outcome["gross_usdg"], "0.002272")
        with self.assertRaises(ledger.LedgerError) as err:
            ledger.settle(row["id"], EXIT_BLOCK, EXIT_TS, "0.068277",
                          outcome["gross_usdg"], path=self.path, cfg=CFG)
        self.assertIn("min_hold_minutes", str(err.exception))


# --------------------------------------------------------------------------
# Uniswap v3
# --------------------------------------------------------------------------

class TestV3ImpermanentLoss(unittest.TestCase):
    def row(self, size="250.000000"):
        return {"id": "x", "kind": "uniswap_v3_pool", "size_usdg": size,
                "entry_ts": ENTRY_TS, "entry_state": V3["entry_state"]}

    def test_the_position_is_revalued_at_the_exit_price(self):
        """ETH moved -0.054% against USDG between the two blocks. A full-range
        position keeps half its value on each side, so it gives back about a
        quarter of the square of that move - 0.067501 USDG on 250 - while a
        wallet holding the same two tokens apart would have lost half the
        move. That gap is the impermanent loss and it is charged before a
        single unit of gas."""
        out = settle.realized_gross(self.row(), V3["exit_state"], EXIT_TS)
        self.assertEqual(out["detail"]["value_at_exit_usdg"], "249.932499")
        self.assertEqual(out["detail"]["impermanent_loss_usdg"], "-0.067501")
        self.assertAlmostEqual(float(out["detail"]["price_move_pct"]), -0.05399, places=4)
        self.assertEqual(out["gross_usdg"], "-0.067473")

    def test_fees_are_the_positions_share_of_the_pools_fee_growth(self):
        out = settle.realized_gross(self.row(), V3["exit_state"], EXIT_TS)
        self.assertEqual(out["detail"]["fees_usdg"], "0.000028")
        # 250 USDG against 33.4M of pool: the fees are real and irrelevant next
        # to the price move, which is the honest shape of an LP position.
        self.assertLess(float(out["detail"]["fees_usdg"]),
                        abs(float(out["detail"]["impermanent_loss_usdg"])))

    def test_a_price_that_did_not_move_realizes_fees_and_nothing_else(self):
        flat = dict(V3["exit_state"],
                    sqrt_price_x96=V3["entry_state"]["sqrt_price_x96"])
        out = settle.realized_gross(self.row(), flat, EXIT_TS)
        self.assertEqual(out["detail"]["impermanent_loss_usdg"], "0.000000")
        self.assertEqual(out["gross_usdg"], out["detail"]["fees_usdg"])

    def test_the_outcome_scales_with_the_size_of_the_position(self):
        big = settle.realized_gross(self.row("250.000000"), V3["exit_state"], EXIT_TS)
        small = settle.realized_gross(self.row("50.000000"), V3["exit_state"], EXIT_TS)
        self.assertAlmostEqual(float(big["gross_usdg"]) / 5,
                               float(small["gross_usdg"]), places=6)


class TestV3FeeDilution(unittest.TestCase):
    """Forecast row f002416, 250 USDG on pool 0x9b050cb1 for a day, recorded
    from chain 4663 at blocks 114,043,132 and 114,847,770. The pool held only
    621,495,628 of in-range liquidity; the simulated position would have held
    about 2.87e12, some 4,600 times more. `feeGrowthGlobal` is fees per unit of
    the pool's liquidity, so crediting it on the position's undiluted
    liquidity paid the position 875.38 USDG of fees, 4,600 times what the
    whole pool earned, and settled the row at an APY of 1.4e236."""

    ENTRY = {"eth_usdg": "1893.2639429355058",
             "fee_growth_0": "39497439944703466140270712112646460392121",
             "fee_growth_1": "178518531783662231402728035681583",
             "liquidity": "621495628", "other_decimals": "18",
             "other_raw": "1170255852764900242106",
             "other_token": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
             "sqrt_price_x96": "3453092286105783430240814", "tick": "-200827",
             "usdg_is_token0": "False", "usdg_raw": "6755026829"}
    EXIT = {"eth_usdg": "1816.906260068923",
            "fee_growth_0": "58484342439227708350458552213926531728896718",
            "fee_growth_1": "185089074958263445078637434487748",
            "liquidity": "621495628", "other_decimals": "18",
            "other_raw": "1170256071151413998077",
            "other_token": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
            "sqrt_price_x96": "3339795854365348113402462", "tick": "-201494",
            "usdg_is_token0": "False", "usdg_raw": "6755177206"}

    def row(self, entry=None):
        return {"id": "f002416", "kind": "uniswap_v3_pool", "size_usdg": "250.000000",
                "entry_ts": 1794646550, "entry_state": entry or self.ENTRY}

    def pool_fees_usdg(self) -> float:
        """Everything the pool's in-range liquidity earned over the hold."""
        pool = int(self.ENTRY["liquidity"])
        d0 = (int(self.EXIT["fee_growth_0"]) - int(self.ENTRY["fee_growth_0"])) % settle.UINT256
        d1 = (int(self.EXIT["fee_growth_1"]) - int(self.ENTRY["fee_growth_1"])) % settle.UINT256
        price = (int(self.EXIT["sqrt_price_x96"]) / settle.Q96) ** 2
        return (d1 / settle.Q128 * pool + d0 / settle.Q128 * pool * price) / 1e6

    def test_a_position_far_larger_than_the_pool_earns_its_share_of_the_pools_fees(self):
        """Price fell 6.45%, so the position lost 8.20 USDG to impermanent
        loss. Holding 99.98% of the liquidity it would have taken 99.98% of
        the pool's 0.1897 USDG of fees, not 875.38 USDG."""
        out = settle.realized_gross(self.row(), self.EXIT, 1794733381)
        self.assertEqual(out["detail"]["pool_liquidity"], "621495628")
        self.assertAlmostEqual(float(out["detail"]["fee_dilution"]), 0.000216652, places=9)
        self.assertEqual(out["detail"]["impermanent_loss_usdg"], "-8.202534")
        self.assertEqual(out["detail"]["fees_usdg"], "0.189653")
        self.assertEqual(out["gross_usdg"], "-8.012882")

    def test_the_fee_credit_never_exceeds_what_the_whole_pool_earned(self):
        out = settle.realized_gross(self.row(), self.EXIT, 1794733381)
        self.assertLessEqual(float(out["detail"]["fees_usdg"]), self.pool_fees_usdg())

    def test_a_row_without_the_pools_liquidity_cannot_be_settled(self):
        entry = {k: v for k, v in self.ENTRY.items() if k != "liquidity"}
        with self.assertRaises(settle.SettleError) as err:
            settle.realized_gross(self.row(entry), self.EXIT, 1794733381)
        self.assertIn("liquidity", str(err.exception))


class TestV3Settles(ReplayCase):
    def test_an_lp_position_settles_a_loss_against_a_positive_forecast(self):
        """Forecast +28.41 bps on a 30% gross rate over seven days; realized
        -24.73 bps, because the price moved and the round trip on this pool is
        0.550863 USDG. The whole miss is real and none of it is a rate the
        agent was allowed to copy into a realized field."""
        settled, _ = self.replay("uniswap_v3_pool", "250", 7 * DAY, 7 * DAY)
        self.assertAlmostEqual(settled["forecast_net_bps"], 28.41, places=2)
        self.assertEqual(settled["realized_gross_usdg"], "-0.067473")
        self.assertEqual(settled["exit_cost_usdg"], "0.203023")
        self.assertEqual(settled["realized_usdg"], "-0.618336")
        self.assertAlmostEqual(settled["realized_net_apy"], -0.12114, places=4)
        self.assertLess(settled["realized_net_apy"], settled["forecast_net_apy"])
        self.assertLess(settled["realized_net_apy"], settled["advertised_apy"])


# --------------------------------------------------------------------------
# Idle, and what settlement refuses
# --------------------------------------------------------------------------

class TestIdle(ReplayCase):
    def test_idle_realizes_zero_minus_nothing(self):
        settled, _ = self.replay("idle", "500", 7 * DAY, 7 * DAY)
        self.assertEqual(settled["realized_gross_usdg"], "0.000000")
        self.assertEqual(settled["entry_cost_usdg"], "0.000000")
        self.assertEqual(settled["exit_cost_usdg"], "0.000000")
        self.assertEqual(settled["realized_usdg"], "0.000000")
        self.assertEqual(settled["realized_net_apy"], 0.0)


class TestRefusals(unittest.TestCase):
    def test_a_row_with_no_entry_state_cannot_be_settled(self):
        row = {"id": "a00009", "kind": "morpho_market", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS, "entry_state": {}}
        with self.assertRaises(settle.SettleError) as err:
            settle.realized_gross(row, MORPHO["exit_state"], EXIT_TS)
        self.assertIn("void the row", str(err.exception))

    def test_a_venue_that_returns_no_state_at_exit_cannot_be_settled(self):
        row = {"id": "a00009", "kind": "morpho_market", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS, "entry_state": MORPHO["entry_state"]}
        with self.assertRaises(settle.SettleError):
            settle.realized_gross(row, {}, EXIT_TS)

    def test_a_kind_with_no_settlement_maths_is_refused_rather_than_guessed(self):
        row = {"id": "a00009", "kind": "uniswap_v2_pool", "size_usdg": "250.000000",
               "entry_ts": ENTRY_TS, "entry_state": {"reserve0": "1"}}
        with self.assertRaises(settle.SettleError) as err:
            settle.realized_gross(row, {"reserve0": "2"}, EXIT_TS)
        self.assertIn("no settlement maths", str(err.exception))

    def test_a_stuck_vault_retries_from_its_entry_holder_and_skips_the_rate_scan(self):
        """a00036: the exit quote tries the holder whose withdraw passed at
        entry, and it does not read the vault rate, which settlement never
        uses. Each of those scans is seven days of logs."""
        row = {"id": "a00036", "venue": "0xd42174d3db28b0fa2bd25381c3521b18ae9db490",
               "kind": "erc4626_usdg_vault", "size_usdg": "250.000000", "entry_ts": ENTRY_TS,
               "status": "open", "forecast_horizon_min": 3 * DAY,
               "entry_state": {"share_price": "1000000", "idle_assets": "8837934909",
                               "exit_simulation": {"ok": True, "assets": "500000000",
                                                   "holder": "0xabc"}}}
        stuck = {"block": 1, "ts": EXIT_TS, "settle_state": {
            "share_price": "1001000", "idle_assets": "0",
            "exit_simulation": {"ok": False, "assets": "500000000", "holder": None,
                                "errors": ["0xabc: {'data': '0x96d80433'}"]}}}
        with mock.patch.object(ledger, "live_quote", return_value=stuck) as live:
            with self.assertRaises(settle.SettleError) as err:
                settle.settle_row(row, dry_run=True)
        live.assert_called_once_with(row["venue"], None, size_usdg="250.000000",
                                     holders=["0xabc"], vault_rates=False)
        self.assertIn("0x96d80433", str(err.exception))
        self.assertIn("stays open", str(err.exception))


class TestLossMark(ReplayCase):
    """A position the venue cannot pay back stays open until
    `loss_mark_after_horizon_minutes` past its horizon, then is marked at what
    the venue could pay and re-checked every run (a00065, the Flock vault).

    The exit states are the recorded ones with free liquidity pinned, and the
    exit is replayed at a later clock, as the module docstring describes."""

    GRACE = 1440

    def setUp(self) -> None:
        super().setUp()
        # Settlement prices the exit leg against the recorded gas snapshot,
        # and reads the grace from this test rather than the live config.
        real_trip = cost.round_trip
        for patcher in (
                mock.patch.object(cost, "round_trip", side_effect=lambda kind, size, *_:
                                  real_trip(kind, size, SNAPSHOT, POOL)),
                mock.patch.object(settle, "_cfg", return_value=dict(
                    settle._cfg(), loss_mark_after_horizon_minutes=self.GRACE))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def morpho_row(self) -> dict:
        return ledger.append(open_row("morpho_market", "250", 3 * DAY), self.path, CFG)

    def vault_row(self) -> dict:
        """The a00065 shape: written straight to the file, because the recorded
        fixture has no vault quote to open one through."""
        row = {"id": "a00065", "ts": "2026-10-22T01:36:21Z", "cycle": "866",
               "venue": "0xd42174d3db28b0fa2bd25381c3521b18ae9db490",
               "kind": "erc4626_usdg_vault", "size_usdg": "250.000000",
               "entry_block": 1, "entry_ts": ENTRY_TS, "entry_cost_usdg": "0.025473",
               "advertised_apy": 0.52, "forecast_gross_apy": 0.45,
               "forecast_net_apy": 0.419, "forecast_net_bps": 28.8,
               "forecast_net_usdg": "0.720577", "forecast_horizon_min": 3 * DAY,
               "status": "open",
               "entry_state": {"share_price": "1091564", "idle_assets": "10520644187",
                               "exit_simulation": {"ok": True, "assets": "500000000",
                                                   "holder": "0xd745"}}}
        ledger.save([row], self.path)
        return row

    @staticmethod
    def drained_morpho(free_raw: int) -> dict:
        supply = int(MORPHO["exit_state"]["total_supply_assets"])
        return dict(MORPHO["exit_state"], total_borrow_assets=str(supply - free_raw))

    DRAINED_VAULT = {"share_price": "1098822", "idle_assets": "0",
                     "exit_simulation": {"ok": False, "assets": "500000000", "holder": None,
                                         "errors": ["0xd745b071: {'data': '0x96d80433'}"]}}

    def settle_at(self, row_id: str, state: dict, overdue_min: int) -> dict:
        row = ledger.find(ledger.load(self.path), row_id)
        quote = {"block": EXIT_BLOCK, "ts": ENTRY_TS + (3 * DAY + overdue_min) * 60,
                 "settle_state": state}
        with mock.patch.object(ledger, "live_quote", return_value=quote):
            return settle.settle_row(row, self.path)

    def stored(self, row_id: str) -> dict:
        return ledger.find(ledger.load(self.path), row_id)

    def test_an_unexitable_row_inside_the_grace_stays_open(self):
        row = self.vault_row()
        with self.assertRaises(settle.Unexitable) as caught:
            self.settle_at(row["id"], self.DRAINED_VAULT, self.GRACE - HOUR)
        self.assertIn("stays open", str(caught.exception))
        self.assertEqual(self.stored(row["id"])["status"], "open")
        self.assertNotIn("realized_usdg", self.stored(row["id"]))

    def test_a_drained_vault_past_the_grace_is_marked_at_zero(self):
        """a00065: the vault holds no idle USDG and every simulated withdraw
        reverts, so nothing can be shown to come back. The whole size is
        booked as lost and no exit gas is charged, because no exit is sent."""
        row = self.vault_row()
        out = self.settle_at(row["id"], self.DRAINED_VAULT, self.GRACE + HOUR)
        self.assertEqual(out["status"], "marked")
        self.assertEqual(out["realized_gross_usdg"], "-250.000000")
        self.assertEqual(out["exit_cost_usdg"], "0.000000")
        self.assertEqual(out["realized_usdg"], "-250.025473")
        self.assertEqual(out["realized_net_apy"], -1.0)
        self.assertIn("0x96d80433", out["mark_reason"])
        self.assertIn("25.0 h past the horizon", out["mark_reason"])
        self.assertEqual(out["settle_detail"]["withdrawable_usdg"], "0.000000")
        self.assertEqual(self.stored(row["id"])["status"], "marked")

    def test_a_mark_is_scored_and_stops_counting_against_the_caps(self):
        row = self.vault_row()
        self.settle_at(row["id"], self.DRAINED_VAULT, self.GRACE + HOUR)
        rows = ledger.load(self.path)
        self.assertEqual(ledger.open_rows(rows), [])
        g = score.group(rows, 0.0)
        self.assertEqual((g["n"], g["n_marked"], g["realized_usdg"]), (1, 1, "-250.025473"))
        rep = validate.Report()
        validate.check_settled(rep, rows[0], CFG)
        self.assertEqual(rep.failures, [])

    def test_a_morpho_market_marks_at_its_free_liquidity(self):
        """A Morpho withdraw pays up to supply minus borrow, so a market with
        100 USDG free marks a 250 USDG position at 100 and charges the exit."""
        row = self.morpho_row()
        out = self.settle_at(row["id"], self.drained_morpho(100_000_000), self.GRACE + HOUR)
        self.assertEqual(out["status"], "marked")
        self.assertEqual(out["realized_gross_usdg"], "-150.000000")
        self.assertEqual(out["exit_cost_usdg"], trip("morpho_market", 250.0)["exit_cost_usdg"])
        self.assertEqual(out["settle_detail"]["withdrawable_usdg"], "100.000000")

    def test_an_exitable_row_past_the_grace_settles_normally(self):
        row = self.morpho_row()
        out = self.settle_at(row["id"], MORPHO["exit_state"], self.GRACE + DAY)
        self.assertEqual(out["status"], "settled")
        self.assertNotIn("mark_reason", out)
        self.assertNotIn("loss_mark", out)
        self.assertGreater(float(out["realized_gross_usdg"]), 0)

    def test_a_marked_row_still_unexitable_is_left_alone(self):
        row = self.vault_row()
        self.settle_at(row["id"], self.DRAINED_VAULT, self.GRACE + HOUR)
        before = self.stored(row["id"])
        out = self.settle_at(row["id"], self.DRAINED_VAULT, self.GRACE + 2 * DAY)
        self.assertEqual(out["status"], "marked")
        self.assertIn("still not exitable", out["recheck"])
        self.assertEqual(self.stored(row["id"]), before)

    def test_a_marked_row_settles_over_its_real_hold_when_the_venue_recovers(self):
        """a00036 recovered 111 h past its horizon. The real settlement
        replaces the mark, the mark is kept beside it, and the row keeps the
        close time the book has been replayed against since the mark."""
        row = self.morpho_row()
        self.settle_at(row["id"], self.drained_morpho(0), self.GRACE + HOUR)
        marked = self.stored(row["id"])
        self.assertIn(marked, settle.due(ledger.load(self.path), cfg=CFG))
        out = self.settle_at(row["id"], MORPHO["exit_state"], 5 * DAY)
        self.assertEqual(out["status"], "settled")
        self.assertGreater(float(out["realized_gross_usdg"]), 0)
        self.assertEqual(out["hold_minutes"], 8 * DAY)
        self.assertEqual(out["loss_mark"]["realized_gross_usdg"], "-250.000000")
        self.assertEqual(out["loss_mark"]["mark_reason"], marked["mark_reason"])
        self.assertEqual(out["settled_ts"], marked["settled_ts"])
        self.assertIn("recovered_ts", out)
        self.assertNotIn("mark_reason", out)
        self.assertEqual(settle.due(ledger.load(self.path), cfg=CFG), [])


class TestDue(unittest.TestCase):
    def rows(self, horizon_min: int) -> list[dict]:
        return [dict(open_row("morpho_market", "250", horizon_min), id="a00001",
                     status="open")]

    def test_a_row_inside_its_horizon_is_not_due(self):
        rows = self.rows(7 * DAY)
        self.assertEqual(settle.due(rows, ENTRY_TS + 3 * 86400, CFG), [])

    def test_a_row_past_its_horizon_is_due(self):
        rows = self.rows(7 * DAY)
        self.assertEqual(len(settle.due(rows, ENTRY_TS + 8 * 86400, CFG)), 1)

    def test_the_hold_floor_sits_under_the_horizon(self):
        """A row can only carry a horizon at or above `min_hold_minutes`, but
        `due` applies the floor again rather than trusting the row."""
        rows = self.rows(7 * DAY)
        rows[0]["forecast_horizon_min"] = 1
        self.assertEqual(settle.due(rows, ENTRY_TS + 30 * 60, CFG), [])
        self.assertEqual(len(settle.due(rows, ENTRY_TS + 61 * 60, CFG)), 1)

    def test_a_settled_row_is_never_due_again(self):
        rows = self.rows(7 * DAY)
        rows[0]["status"] = "settled"
        self.assertEqual(settle.due(rows, ENTRY_TS + 8 * 86400, CFG), [])


if __name__ == "__main__":
    unittest.main()
