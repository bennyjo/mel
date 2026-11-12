"""The dormant real leg: that it refuses, and that the calldata it would build
is right.

PROTECTED ENGINE FILE. The allocating agent may not edit this file.

Two things are worth testing about code that has never run. First, the gate:
every fund-touching command refuses with today's config, and the gate is a real
gate rather than a constant, which is what the cleared-config test proves.
Second, the calldata: `build_steps` is pure, so the exact bytes a real
allocation would sign can be checked today, offline, against a market recorded
from chain 4663.

`rpc.forbid_network` is on for the whole module. Nothing here reaches the
chain, and nothing reaches a signer: there is no signer.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core import abi, real, rpc

rpc.forbid_network("core/tests/test_real.py never touches the chain")

CFG = json.loads((ROOT / "config" / "protected.json").read_text())

# The deepest USDG Morpho market on 4663, from journal/venues.jsonl at block
# 58,050,956. Its id reproduces from these five parameters, which is what
# `build_steps` checks before it will build a supply.
MARKET = {
    "address": "0x9d53d5e3bd5e8d4cbfa6db1ca238aea02e651010",
    "market_id": "0xc845da65a020ddca5f132efa8fea79676d8edfdea504226a4c01e7a9e34cddd6",
    "collateral_token": "0x5d3a1ff2b6bab83b63cd9ad0787074081a52ef34",
    "oracle": "0xe64849bd4ad03dfabbe02bb521de19997a19055f",
    "irm": "0x2bd3d5965b26b51814ac95127b2b80dd6ccc0fa1",
    "lltv": "0.915000",
    "kind": "morpho_market",
}

SAFE = "0x00000000000000000000000000000000000f0000"


def args(calldata: str) -> str:
    """The argument words of a call, with the four-byte selector removed.
    `core/abi.py`'s decoders read return data, which carries no selector."""
    return "0x" + calldata[10:]


def cleared_cfg() -> dict:
    """A config with every operator gate opened, for testing the gate itself.
    It is not, and must never become, `config/protected.json`."""
    cfg = copy.deepcopy(CFG)
    cfg["real_execution_enabled"] = True
    cfg["real"]["olas_registries"] = {"service_registry": SAFE}
    cfg["real"]["allowed_venues"] = [MARKET["address"]]
    cfg["real"]["service_safe"] = SAFE
    return cfg


def morpho_row(size: str = "250.000000") -> dict:
    return {"id": "a00001", "venue": MARKET["address"], "kind": "morpho_market",
            "market_id": MARKET["market_id"], "size_usdg": size}


class TestGate(unittest.TestCase):
    def test_todays_config_blocks_everything(self):
        gates = real.blockers(CFG, env={})
        blocked = {g["gate"] for g in gates if g["blocked"]}
        self.assertEqual(blocked, {"real_execution_enabled",
                                   "allowed_venues", "pearl_connect_store"})
        self.assertFalse(real.ready(CFG, env={}))

    def test_every_gate_is_the_operators_to_clear(self):
        # Since Pearl Connect runs on chain 4663 (2026-10-06) no gate waits on
        # the chain; each is an operator act the agent can only ask for.
        owners = {g["gate"]: g["owner"] for g in real.blockers(CFG, env={})}
        self.assertEqual(set(owners.values()), {"operator"})

    def test_smoke_is_capped_and_withdraws_by_shares(self):
        with self.assertRaises(real.RealRefused):
            real.smoke_steps(MARKET, 1.01, SAFE, CFG)
        steps = real.smoke_steps(MARKET, 1.0, SAFE, CFG, supply_shares=123)
        self.assertEqual(list(steps), ["approve", "supply", "withdraw"])
        w = steps["withdraw"]["data"]
        self.assertTrue(w.startswith(real.abi.selector(real.WITHDRAW_SIG)))
        # assets 0, shares 123: withdraw everything, by shares
        words = w[10:]
        self.assertEqual(int(words[5*64:6*64], 16), 0)
        self.assertEqual(int(words[6*64:7*64], 16), 123)

    def test_require_ready_raises_with_todays_config(self):
        with self.assertRaises(real.RealRefused) as caught:
            real.require_ready(CFG, env={})
        self.assertIn("real_execution_enabled", str(caught.exception))

    def test_the_gate_opens_when_every_blocker_clears(self):
        # Without this the gate could be a constant `False` and the other
        # tests would still pass.
        store = ROOT / "core" / "tests" / "fixtures"
        (store / ".mcp.json").write_text("{}")
        try:
            env = {real.STORE_ENV: str(store)}
            self.assertTrue(real.ready(cleared_cfg(), env),
                            real.blockers(cleared_cfg(), env))
            real.require_ready(cleared_cfg(), env)
        finally:
            (store / ".mcp.json").unlink()

    def test_a_missing_mcp_json_still_blocks(self):
        env = {real.STORE_ENV: str(ROOT / "work" / "does-not-exist")}
        blocked = {g["gate"] for g in real.blockers(cleared_cfg(), env)
                   if g["blocked"]}
        self.assertEqual(blocked, {"pearl_connect_store"})


class TestCalldata(unittest.TestCase):
    def test_idle_builds_no_transaction(self):
        row = {"id": "a00002", "venue": "idle", "kind": "idle",
               "size_usdg": "750.000000"}
        self.assertEqual(real.build_steps(row, cfg=cleared_cfg()), [])

    def test_an_lp_position_is_refused_not_approximated(self):
        row = {"id": "a00003", "venue": "0x52e6", "kind": "uniswap_v3_pool",
               "size_usdg": "250.000000"}
        with self.assertRaises(real.RealRefused):
            real.build_steps(row, cfg=cleared_cfg())

    def test_no_service_safe_refuses(self):
        cfg = cleared_cfg()
        cfg["real"].pop("service_safe")
        with self.assertRaises(real.RealRefused) as caught:
            real.build_steps(morpho_row(), MARKET, cfg)
        self.assertIn("service_safe", str(caught.exception))

    def test_morpho_supply_calldata(self):
        steps = real.build_steps(morpho_row(), MARKET, cleared_cfg())
        self.assertEqual([s["step"] for s in steps], ["approve", "supply"])

        raw = 250_000_000  # 250 USDG at the token's six decimals
        approve = steps[0]
        self.assertEqual(approve["to"].lower(),
                         CFG["quote_asset_address"].lower())
        self.assertEqual(approve["data"][:10],
                         abi.SELECTORS["approve(address,uint256)"])
        self.assertEqual(abi.decode_address(args(approve["data"]), 0).lower(),
                         MARKET["address"])
        self.assertEqual(abi.decode_uint(args(approve["data"]), 1), raw)

        supply = steps[1]
        self.assertEqual(supply["to"], MARKET["address"])
        self.assertEqual(supply["data"][:10], abi.SELECTORS[real.SUPPLY_SIG])
        # Five inline market-parameter words, then assets, shares, onBehalf,
        # and the offset to an empty `bytes` tail.
        self.assertEqual(abi.decode_address(args(supply["data"]), 0).lower(),
                         CFG["quote_asset_address"].lower())
        self.assertEqual(abi.decode_address(args(supply["data"]), 1).lower(),
                         MARKET["collateral_token"])
        self.assertEqual(abi.decode_address(args(supply["data"]), 2).lower(),
                         MARKET["oracle"])
        self.assertEqual(abi.decode_address(args(supply["data"]), 3).lower(),
                         MARKET["irm"])
        self.assertEqual(abi.decode_uint(args(supply["data"]), 4),
                         915 * 10 ** 15)
        self.assertEqual(abi.decode_uint(args(supply["data"]), 5), raw)
        self.assertEqual(abi.decode_uint(args(supply["data"]), 6), 0,
                         "shares must be zero: the supply is by assets")
        self.assertEqual(abi.decode_address(args(supply["data"]), 7).lower(), SAFE)
        self.assertEqual(abi.decode_uint(args(supply["data"]), 8), 9 * 32)
        self.assertEqual(abi.decode_uint(args(supply["data"]), 9), 0)
        self.assertEqual(len(supply["data"]), 2 + 8 + 10 * 64)

    def test_supply_refuses_parameters_that_hash_to_another_market(self):
        wrong = dict(MARKET, oracle="0x" + "11" * 20)
        with self.assertRaises(real.RealRefused) as caught:
            real.build_steps(morpho_row(), wrong, cleared_cfg())
        self.assertIn("did not choose", str(caught.exception))

    def test_a_morpho_row_without_its_venue_record_refuses(self):
        with self.assertRaises(real.RealRefused):
            real.build_steps(morpho_row(), None, cleared_cfg())

    def test_vault_deposit_calldata(self):
        row = {"id": "a00004", "venue": "0xbeeff033f34c046626b8d0a041844c5d1a5409dd",
               "kind": "erc4626_usdg_vault", "size_usdg": "12.500000"}
        steps = real.build_steps(row, cfg=cleared_cfg())
        self.assertEqual([s["step"] for s in steps], ["approve", "deposit"])
        deposit = steps[1]
        self.assertEqual(deposit["data"][:10],
                         abi.SELECTORS["deposit(uint256,address)"])
        self.assertEqual(abi.decode_uint(args(deposit["data"]), 0), 12_500_000)
        self.assertEqual(abi.decode_address(args(deposit["data"]), 1).lower(), SAFE)

    def test_the_amount_comes_from_the_string_not_a_float(self):
        # 0.1 + 0.2 in binary floating point is 0.30000000000000004; the row
        # carries "0.300000" and the calldata must carry exactly 300000.
        row = {"id": "a00005", "venue": "0xbeef", "kind": "erc4626_usdg_vault",
               "size_usdg": "0.300000"}
        steps = real.build_steps(row, cfg=cleared_cfg())
        self.assertEqual(abi.decode_uint(args(steps[1]["data"]), 0), 300_000)


if __name__ == "__main__":
    unittest.main()
