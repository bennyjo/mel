"""The pure part of `core/venues.py`: which recorded rows a cycle quotes,
and whom a vault exit is simulated from.

Offline. The chain-reading half of the module is exercised by the cycle
itself; this covers the ranking that decides where the quote budget goes,
and, against a fake vault, the order `exit_simulation` asks holders in.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import abi, rpc, venues

ROWS = [
    {"address": "0xdeep", "kind": "morpho_market", "depth_usdg": "1000000", "advertised_apy": 0.05},
    {"address": "0xhot", "kind": "morpho_market", "depth_usdg": "360000", "advertised_apy": 0.81},
    {"address": "0xdust", "kind": "morpho_market", "depth_usdg": "900", "advertised_apy": 2.5},
    {"address": "0xvault", "kind": "erc4626_usdg_vault", "total_assets_usdg": "500000", "advertised_apy": 0.12},
    {"address": "0xblank", "kind": "uniswap_v3_pool", "advertised_apy": None},
]


class TestRank(unittest.TestCase):
    def test_depth_is_the_default_and_takes_the_deepest(self):
        picked = [r["address"] for r in venues.rank(ROWS, top=2)]
        self.assertEqual(picked, ["0xdeep", "0xvault"])

    def test_rate_takes_the_highest_advertised_rate(self):
        picked = [r["address"] for r in venues.rank(ROWS, by="rate", top=2)]
        self.assertEqual(picked, ["0xdust", "0xhot"])

    def test_min_depth_is_applied_before_the_pick_not_after(self):
        """The cycle-1 complaint: top-N then filter returns fewer than N and
        misses rows that pass the floor. Filter then top-N does not."""
        picked = [r["address"] for r in venues.rank(ROWS, by="rate", top=2, min_depth=10_000)]
        self.assertEqual(picked, ["0xhot", "0xvault"])

    def test_rows_without_a_recorded_depth_count_as_zero(self):
        picked = [r["address"] for r in venues.rank(ROWS, min_depth=1)]
        self.assertNotIn("0xblank", picked)
        self.assertEqual(len(venues.rank(ROWS)), len(ROWS))

    def test_an_unknown_ranking_is_refused(self):
        with self.assertRaises(ValueError):
            venues.rank(ROWS, by="vibes")


class FakeVault:
    """A vault priced 1 USDG raw per 10 share raw that pays a withdraw only
    from its idle USDG and only to a holder with the shares for it. Its
    revert data is the Flock vault's `InsufficientAssets()` (a00036)."""

    ADDRESS = "0xvault"
    TOTALS = {"total_assets": 1_000_000_000, "total_shares": 10_000_000_000}

    def __init__(self, idle: int, shares: dict[str, int], depositors: list[str]):
        self.idle, self.shares, self.depositors = idle, shares, depositors
        self.scans = 0
        self.callers: list[str] = []

    def eth_call(self, to, data, block="latest", from_=None):
        self.callers.append(from_)
        assets = abi.decode_uint("0x" + data[10:], 0)
        if self.shares.get(from_, 0) // 10 < assets:
            raise rpc.RpcError("eth_call[...]: {'data': '0xe450d38c'}")
        if assets > self.idle:
            raise rpc.RpcError("eth_call[...]: {'data': '0x96d80433'}")
        return "0x" + "0" * 64

    def eth_call_batch(self, calls, block="latest", allow_errors=True):
        return ["0x" + abi.encode_uint(self.shares.get("0x" + data[-40:], 0)).hex()
                for _to, data in calls]

    def recent_depositors(self, vault, head, limit=3):
        self.scans += 1
        return self.depositors[:limit]

    def simulate(self, assets: int, known: list[str], totals: dict | None = TOTALS) -> dict:
        with mock.patch.object(rpc, "eth_call", self.eth_call), \
                mock.patch.object(rpc, "eth_call_batch", self.eth_call_batch), \
                mock.patch.object(venues, "recent_depositors", self.recent_depositors):
            return venues.exit_simulation(self.ADDRESS, assets, 100, known, totals)


RICH = "0x" + "1" * 40      # shares worth 3,000 USDG raw
POOR = "0x" + "2" * 40      # shares worth 100 USDG raw
OTHER = "0x" + "3" * 40     # a depositor only the scan finds, 5,000 USDG raw
GONE = "0x" + "4" * 40      # deposited the most, has since redeemed every share
SHARES = {RICH: 30_000, POOR: 1_000, OTHER: 50_000, GONE: 0}


class TestExitSimulation(unittest.TestCase):
    def test_a_known_holder_whose_withdraw_passes_settles_it_without_a_scan(self):
        vault = FakeVault(idle=10_000, shares=SHARES, depositors=[OTHER])
        sim = vault.simulate(500, [RICH])
        self.assertTrue(sim["ok"])
        self.assertEqual(sim["holder"], RICH)
        self.assertEqual(vault.scans, 0)

    def test_a_revert_from_a_holder_with_enough_shares_is_the_vaults_without_a_scan(self):
        """a00036: the vault holds no idle USDG, and the holder whose withdraw
        passed at entry still holds far more than the withdraw. Its revert
        says the vault cannot pay; a scan would find nothing better."""
        vault = FakeVault(idle=0, shares=SHARES, depositors=[OTHER])
        sim = vault.simulate(500, [RICH])
        self.assertFalse(sim["ok"])
        self.assertIn("0x96d80433", sim["errors"][0])
        self.assertIn("the revert is the vault's", sim["note"])
        self.assertEqual(vault.scans, 0)

    def test_a_holder_without_the_shares_cannot_decide_so_the_scan_runs(self):
        vault = FakeVault(idle=10_000, shares=SHARES, depositors=[OTHER])
        sim = vault.simulate(500, [POOR])
        self.assertTrue(sim["ok"])
        self.assertEqual(sim["holder"], OTHER)
        self.assertEqual(vault.scans, 1)

    def test_unpriced_shares_prove_nothing_so_the_scan_is_inconclusive(self):
        vault = FakeVault(idle=0, shares=SHARES, depositors=[OTHER])
        sim = vault.simulate(500, [RICH], totals={"total_assets": None, "total_shares": None})
        self.assertIsNone(sim["ok"])
        self.assertEqual(vault.scans, 1)
        self.assertIn("totals were not read", sim["note"])

    def test_a_depositor_who_has_redeemed_is_skipped(self):
        """E-2026-11-21-a: the largest recent depositor has since redeemed.
        Asked to withdraw, it reverts on its own balance (Spark
        `insufficient-balance`), which says nothing about the vault. The
        depositor that still holds the shares decides."""
        vault = FakeVault(idle=10_000, shares=SHARES, depositors=[GONE, POOR, OTHER])
        sim = vault.simulate(500, [])
        self.assertTrue(sim["ok"])
        self.assertEqual(sim["holder"], OTHER)
        self.assertEqual(vault.callers, [OTHER])

    def test_the_scan_looks_past_the_three_largest_depositors(self):
        vault = FakeVault(idle=10_000, shares=SHARES, depositors=[GONE, GONE, POOR, OTHER])
        sim = vault.simulate(500, [])
        self.assertTrue(sim["ok"])
        self.assertEqual(sim["holder"], OTHER)

    def test_when_every_depositor_has_redeemed_the_simulation_is_inconclusive(self):
        """Nobody left to ask is not the vault refusing: `ok` is None, no
        withdraw is simulated, and the note says why."""
        vault = FakeVault(idle=10_000, shares=SHARES, depositors=[GONE, POOR])
        sim = vault.simulate(500, [])
        self.assertIsNone(sim["ok"])
        self.assertEqual(vault.callers, [])
        self.assertIn("inconclusive", sim["note"])
        self.assertIn("none of the 2 largest recent depositors", sim["note"])

    def test_no_depositor_at_all_is_inconclusive(self):
        vault = FakeVault(idle=10_000, shares=SHARES, depositors=[])
        sim = vault.simulate(500, [])
        self.assertIsNone(sim["ok"])
        self.assertIn("no recent depositor found", sim["note"])

    def test_a_revert_from_a_depositor_who_covers_the_withdraw_is_still_not_exitable(self):
        """The rule stays as strict: a depositor whose shares cover the
        withdraw and whose withdraw reverts is the vault refusing."""
        vault = FakeVault(idle=0, shares=SHARES, depositors=[GONE, OTHER, POOR])
        sim = vault.simulate(500, [])
        self.assertIs(sim["ok"], False)
        self.assertEqual(vault.callers, [OTHER])
        self.assertIn("0x96d80433", sim["errors"][0])
        self.assertIn("the revert is the vault's", sim["note"])


if __name__ == "__main__":
    unittest.main()
