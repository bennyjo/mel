"""Real execution through Pearl Connect. Dormant, and the only code that may
ever touch funds.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

Nothing in this file has ever sent a transaction on chain 4663 and nothing in
it can, today. `real_execution_enabled` in `config/protected.json` is false,
`core/validate.py` fails the build if it is ever true, and every command below
except `doctor` refuses before it reads anything.

The shape it is built to, from `journal/operator-notes.md`: Pearl Connect runs
a local signer that exposes an MCP surface - `wallet_info`,
`preflight_transaction`, `safe_transaction`, `transaction_status` - and signs
as the service safe. Mel therefore never holds a key and never sees one. It
builds calldata, hands it to the signer, and reads back a hash. That division
is the whole reason this file is small: it owns the gate and the calldata, and
owns no key material, no strategy, and no accounting.

Since 2026-10-06 Pearl Connect runs on chain 4663 (service #12, service safe
in `real.service_safe`), so the chain is no longer the blocker. Allocation
stays dormant until the operator enables it; the one path that moves funds
before that is `smoke`, an operator-run, step-by-step, 1-USDG round trip that
proves the send path against the live signer.

The gate is at the command boundary; the calldata builder below it is pure and
offline, so it can be tested without a signer and without a chain.

Commands:
    python3 core/real.py doctor [--json]
    python3 core/real.py plan --id a00001       # refused while dormant
    python3 core/real.py execute --id a00001    # refused while dormant
    python3 core/real.py status --tx 0xabc      # refused while dormant
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import abi, ledger, rpc, venues

# The MCP tools Pearl Connect exposes. Named here so the day the registries
# land, the operator can see exactly which surface this file expects and
# nothing has to be inferred from the code.
MCP_TOOLS = (
    "wallet_info",           # read-only: safe address, owner, balances
    "preflight_transaction",  # guardrail POLICY check only: "allowed" means the
                              # guardrail permits the bytes, not that the call
                              # succeeds. On-chain simulation is our eth_call.
    "safe_transaction",      # sign and send as the service safe
    "transaction_status",    # poll a submitted hash
)

# Pearl Connect writes its MCP config into a workspace directory named by this
# environment variable. Absent, there is no signer to talk to.
STORE_ENV = "PEARL_CONNECT_STORE"

# Venue kinds this file knows how to build calldata for. A kind absent here is
# refused rather than approximated: an unsupported venue must not silently
# become an approve-and-hope.
SUPPORTED_KINDS = ("morpho_market", "erc4626_usdg_vault")

SUPPLY_SIG = ("supply((address,address,address,address,uint256),uint256,"
              "uint256,address,bytes)")


class RealRefused(Exception):
    """A command that will not run. Nothing was read, sent, or written."""


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def blockers(cfg: dict | None = None, env: dict | None = None) -> list[dict]:
    """Every reason real execution is not ready, in the order they must be
    cleared. Pure: reads the config and the environment, never the chain and
    never the signer.

    A blocker is `{"gate", "blocked", "detail", "owner"}`. `owner` is who can
    clear it, which is the only thing the agent can act on: an `operator`
    blocker belongs in `journal/proposals.md`, and a `chain` blocker is not
    anyone's to clear until Robinhood Chain has the contracts.
    """
    cfg = cfg if cfg is not None else rpc.protected_config()
    env = env if env is not None else dict(os.environ)
    real = cfg.get("real") or {}
    out = []

    enabled = cfg.get("real_execution_enabled") is True
    out.append({
        "gate": "real_execution_enabled",
        "blocked": not enabled,
        "detail": "config/protected.json sets real_execution_enabled false. "
                  "Turning it on is an operator act and a protected-file edit.",
        "owner": "operator",
    })

    safe = real.get("service_safe") or ""
    out.append({
        "gate": "service_safe",
        "blocked": not (isinstance(safe, str) and len(safe) == 42),
        "detail": "config/protected.json names no real.service_safe. Pearl "
                  "Connect signs as the service safe of the Robinhood Chain "
                  "Connect service; put its address here."
                  if not safe else f"real.service_safe={safe}",
        "owner": "operator",
    })

    allowed = real.get("allowed_venues") or []
    out.append({
        "gate": "allowed_venues",
        "blocked": not allowed,
        "detail": "config/protected.json sets real.allowed_venues to []. No "
                  "venue is cleared for real money until the operator names "
                  "one, whatever the paper ledger says about it.",
        "owner": "operator",
    })

    store = env.get(STORE_ENV, "")
    store_ok = bool(store) and (Path(store).expanduser() / ".mcp.json").is_file()
    out.append({
        "gate": "pearl_connect_store",
        "blocked": not store_ok,
        "detail": f"{STORE_ENV} is not set to a Pearl Connect workspace "
                  f"holding .mcp.json, so the signer's MCP surface "
                  f"({', '.join(MCP_TOOLS)}) is not reachable."
                  if not store_ok else f"{STORE_ENV}={store}",
        "owner": "operator",
    })

    return out


def ready(cfg: dict | None = None, env: dict | None = None) -> bool:
    return not any(b["blocked"] for b in blockers(cfg, env))


def require_ready(cfg: dict | None = None, env: dict | None = None) -> None:
    """The gate every fund-touching command passes through first. It raises
    before reading the ledger, the chain, or the signer, so a command that is
    not ready has no side effect of any kind."""
    stops = [b for b in blockers(cfg, env) if b["blocked"]]
    if stops:
        lines = "\n  - ".join(f"{b['gate']}: {b['detail']}" for b in stops)
        raise RealRefused(
            "real execution is dormant and this command will not run:\n  - "
            + lines)


# --------------------------------------------------------------------------
# Calldata. Pure, offline, and testable without a signer.
# --------------------------------------------------------------------------

def _raw_amount(size_usdg: object, decimals: int) -> int:
    """The row's decimal-string amount as the token's raw integer. One
    conversion, in one place, never through a float."""
    return abi.str_to_fixed(str(size_usdg), decimals)


def _market_words(market: dict, loan_token: str) -> bytes:
    """Morpho's five market-parameter words, in the contract's own order."""
    return (abi.encode_address(loan_token)
            + abi.encode_address(market["collateral_token"])
            + abi.encode_address(market["oracle"])
            + abi.encode_address(market["irm"])
            + abi.encode_uint(abi.str_to_fixed(market["lltv"], 18)))


def build_steps(row: dict, market: dict | None = None,
                cfg: dict | None = None) -> list[dict]:
    """The transactions one paper row would become, in order.

    Pure. It builds calldata from the row, the venue record, and the config,
    and returns it. It does not price, sign, send, or record anything, which is
    what lets it be tested without a signer and without a chain.

    `market` is the `journal/venues.jsonl` record for a Morpho market, which is
    where the five market parameters live; the ledger row carries the market
    state, not the parameters. The market id is recomputed from those five
    words before any calldata is built, because supplying against parameters
    that hash to a different id is how funds land in a market nobody chose.
    """
    cfg = cfg if cfg is not None else rpc.protected_config()
    kind = row.get("kind")
    if kind == "idle":
        return []
    if kind not in SUPPORTED_KINDS:
        raise RealRefused(
            f"kind {kind!r} has no real-execution path in core/real.py. "
            f"Supported: {', '.join(SUPPORTED_KINDS)}. An unsupported venue is "
            f"refused, never approximated.")

    token = cfg["quote_asset_address"]
    amount = _raw_amount(row["size_usdg"], int(cfg["quote_asset_decimals"]))
    safe = (cfg.get("real") or {}).get("service_safe")
    if not safe:
        raise RealRefused(
            "config/protected.json names no real.service_safe, so there is no "
            "address to supply on behalf of. Pearl Connect signs as the "
            "service safe and that safe does not exist on chain 4663 yet.")
    venue = row["venue"]

    steps = [{
        "step": "approve",
        "to": token,
        "value": 0,
        "data": abi.encode_call("approve(address,uint256)", venue, amount),
        "why": f"let {venue} pull {row['size_usdg']} USDG",
    }]

    if kind == "morpho_market":
        if not market:
            raise RealRefused(
                f"row {row.get('id')} is a Morpho supply and no venue record "
                f"was given, so the five market parameters are unknown.")
        words = _market_words(market, token)
        want = str(row.get("market_id") or market.get("market_id") or "").lower()
        got = "0x" + abi.keccak256(words).hex()
        if got != want:
            raise RealRefused(
                f"the market parameters hash to {got}, not the row's market "
                f"{want}. Refusing to supply into a market the row did not "
                f"choose.")
        # `supply(MarketParams,assets,shares,onBehalf,data)`. Built by hand
        # rather than through `encode_call`, which takes static types only: the
        # struct is five inline words and `data` is empty bytes, whose head is
        # the offset to its own tail.
        head = (words
                + abi.encode_uint(amount)      # assets
                + abi.encode_uint(0)           # shares: supply by assets, not shares
                + abi.encode_address(safe)     # onBehalf
                + abi.encode_uint(9 * 32))     # offset to `data`
        data = abi.SELECTORS[SUPPLY_SIG][2:] + head.hex() + abi.encode_uint(0).hex()
        steps.append({
            "step": "supply",
            "to": venue,
            "value": 0,
            "data": "0x" + data,
            "why": f"supply {row['size_usdg']} USDG to market {want}",
        })
    else:
        steps.append({
            "step": "deposit",
            "to": venue,
            "value": 0,
            "data": abi.encode_call("deposit(uint256,address)", amount, safe),
            "why": f"deposit {row['size_usdg']} USDG into vault {venue}",
        })
    return steps


# --------------------------------------------------------------------------
# The signer: Pearl Connect's MCP surface over streamable HTTP
# --------------------------------------------------------------------------

def _mcp_session(store: str):
    """A call function bound to one MCP session with the local signer. The
    bearer token is read from the workspace's `.mcp.json` and never printed,
    logged, or written anywhere."""
    import urllib.request
    cfg = json.loads((Path(store).expanduser() / ".mcp.json").read_text())
    server = next(iter(cfg["mcpServers"].values()))
    url, auth = server["url"], server["headers"]["Authorization"]
    state = {"sid": None, "id": 0}

    def post(msg: dict):
        h = {"Authorization": auth, "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if state["sid"]:
            h["Mcp-Session-Id"] = state["sid"]
        req = urllib.request.Request(url, json.dumps(msg).encode(), h)
        with urllib.request.urlopen(req, timeout=300) as r:
            state["sid"] = r.headers.get("Mcp-Session-Id") or state["sid"]
            body = r.read().decode()
        if not body.strip():
            return None
        if body.lstrip().startswith("{"):
            return json.loads(body)
        data = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        return json.loads(data[-1]) if data else None

    post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
          "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                     "clientInfo": {"name": "mel", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(tool: str, args: dict) -> dict:
        state["id"] += 1
        out = post({"jsonrpc": "2.0", "id": state["id"], "method": "tools/call",
                    "params": {"name": tool, "arguments": args}})
        res = (out or {}).get("result") or {}
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content", []))
            raise RealRefused(f"{tool} failed: {text[:400]}")
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        return json.loads(res["content"][0]["text"])
    return call


# --------------------------------------------------------------------------
# Smoke: an operator-run 1-USDG round trip through the live signer
# --------------------------------------------------------------------------

SMOKE_MAX_USDG = 1.0
SMOKE_LOG = ROOT / "journal" / "real-smoke.jsonl"
POSITION_SIG = "position(bytes32,address)"
WITHDRAW_SIG = ("withdraw((address,address,address,address,uint256),uint256,"
                "uint256,address,address)")


def smoke_steps(market: dict, usdg: float, safe: str, cfg: dict,
                supply_shares: int = 0) -> dict:
    """approve, supply and withdraw-all for one Morpho market, as calldata.
    Pure. Withdraw is by shares, so the round trip leaves no dust."""
    if not 0 < usdg <= SMOKE_MAX_USDG:
        raise RealRefused(f"smoke moves at most {SMOKE_MAX_USDG} USDG, not {usdg}")
    token = cfg["quote_asset_address"]
    words = _market_words(market, token)
    got = "0x" + abi.keccak256(words).hex()
    if got != str(market.get("market_id", "")).lower():
        raise RealRefused(f"market parameters hash to {got}, not {market.get('market_id')}")
    venue = market["address"]
    amount = _raw_amount(usdg, int(cfg["quote_asset_decimals"]))
    supply = (abi.SELECTORS[SUPPLY_SIG][2:]
              + (words + abi.encode_uint(amount) + abi.encode_uint(0)
                 + abi.encode_address(safe) + abi.encode_uint(9 * 32)).hex()
              + abi.encode_uint(0).hex())
    withdraw = abi.selector(WITHDRAW_SIG)[2:] + (
        words + abi.encode_uint(0) + abi.encode_uint(supply_shares)
        + abi.encode_address(safe) + abi.encode_address(safe)).hex()
    return {
        "approve": {"to": token, "data": abi.encode_call("approve(address,uint256)", venue, amount),
                    "why": f"let Morpho pull {usdg} USDG"},
        "supply": {"to": venue, "data": "0x" + supply,
                   "why": f"supply {usdg} USDG to market {got[:12]}"},
        "withdraw": {"to": venue, "data": "0x" + withdraw,
                     "why": f"withdraw all {supply_shares} supply shares back to the safe"},
    }


def cmd_smoke(args: argparse.Namespace) -> int:
    """Operator only. Independent of real_execution_enabled on purpose: that
    flag governs the unattended loop, and this is a human-driven round trip
    capped at SMOKE_MAX_USDG. Every step is simulated on chain from the safe
    (eth_call) and checked against the guardrail (preflight) before anything
    is sent, and nothing is sent without --send."""
    cfg = rpc.protected_config()
    safe = (cfg.get("real") or {}).get("service_safe")
    store = os.environ.get(STORE_ENV, "")
    if not safe or not store or not (Path(store).expanduser() / ".mcp.json").is_file():
        raise RealRefused("smoke needs real.service_safe in config/protected.json and "
                          f"{STORE_ENV} pointing at the Connect workspace")
    want = args.market_id.lower()
    market = next((v for v in venues.load() if str(v.get("market_id", "")).lower()
                   .startswith(want)), None)
    if not market:
        raise RealRefused(f"no recorded Morpho market starts with {args.market_id}")
    out = rpc.eth_call(market["address"],
                       abi.encode_call(POSITION_SIG, market["market_id"], safe))
    shares = abi.decode_uint(out, 0)
    steps = smoke_steps(market, args.usdg, safe, cfg, shares)
    step = steps[args.step]
    call = _mcp_session(store)
    try:
        rpc.eth_call(step["to"], step["data"], from_=safe)
        sim = "passes"
    except rpc.RpcError as err:
        sim = f"REVERTS: {str(err).split(']: ', 1)[-1][:200]}"
    pre = call("preflight_transaction", {"chain": "robinhood", "target": step["to"],
                                         "value": 0, "data": step["data"]})
    report = {"step": args.step, "to": step["to"], "why": step["why"],
              "supply_shares_now": shares, "simulation_from_safe": sim,
              "guardrail": pre}
    print(json.dumps(report, indent=1))
    if not args.send:
        print("\nnot sent: pass --send to submit this step")
        return 0
    if sim != "passes" or not pre.get("allowed"):
        raise RealRefused("not sending: the simulation or the guardrail says no")
    sent = call("safe_transaction", {"chain": "robinhood", "target": step["to"],
                                     "value": 0, "data": step["data"],
                                     "wait_for_receipt": True})
    record = {"ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
              "step": args.step, "market_id": market["market_id"], "usdg": args.usdg,
              "to": step["to"], "safe": safe, "result": sent}
    with SMOKE_LOG.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    print(json.dumps(sent, indent=1))
    return 0


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = rpc.protected_config()
    gates = blockers(cfg)
    report = {
        "ready": not any(g["blocked"] for g in gates),
        "chain_id": cfg.get("chain_id"),
        "mcp_tools_expected": list(MCP_TOOLS),
        "real_caps": {k: v for k, v in (cfg.get("real") or {}).items()
                      if not k.startswith("_")},
        "gates": gates,
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"real execution: {'READY' if report['ready'] else 'DORMANT'} "
          f"on chain {report['chain_id']}")
    for gate in gates:
        mark = "BLOCKED" if gate["blocked"] else "ok     "
        print(f"  {mark}  {gate['gate']}  ({gate['owner']})")
        if gate["blocked"]:
            print(f"           {gate['detail']}")
    if not report["ready"]:
        print("\nNo transaction can be built or sent while any gate is blocked.")
    return 0


def _row(row_id: str) -> dict:
    for row in ledger.load():
        if row.get("id") == row_id:
            return row
    raise RealRefused(f"the ledger has no row with id {row_id!r}")


def cmd_plan(args: argparse.Namespace) -> int:
    require_ready()
    row = _row(args.id)
    market = None
    if row.get("kind") == "morpho_market":
        want = str(row.get("market_id", "")).lower()
        market = next((v for v in venues.load()
                       if str(v.get("market_id", "")).lower() == want), None)
    print(json.dumps({"id": args.id, "steps": build_steps(row, market)}, indent=2))
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    require_ready()
    # Unreachable while dormant. When the gates clear, this is where each step
    # goes to `preflight_transaction` and only then to `safe_transaction`, one
    # at a time, with the returned hash polled through `transaction_status`
    # before the next step is built. Mel never signs; the signer does.
    raise RealRefused(
        "execute has no implementation yet. The gates have never opened, so "
        "the send path has never been exercised against a real signer, and "
        "shipping an untested one behind an open gate is how funds are lost. "
        "The operator implements it against a live Pearl Connect service.")


def cmd_status(args: argparse.Namespace) -> int:
    require_ready()
    raise RealRefused(
        "status has no implementation yet: it polls `transaction_status` on "
        "the signer, and there is no signer and no transaction.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="why real execution is or is not ready")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("plan", help="the transactions one ledger row would become")
    p.add_argument("--id", required=True)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("execute", help="send one ledger row for real")
    p.add_argument("--id", required=True)
    p.set_defaults(fn=cmd_execute)

    p = sub.add_parser("smoke", help="OPERATOR ONLY: one step of a 1-USDG Morpho round trip")
    p.add_argument("--market-id", required=True, help="a prefix is enough")
    p.add_argument("--step", required=True, choices=("approve", "supply", "withdraw"))
    p.add_argument("--usdg", type=float, default=SMOKE_MAX_USDG)
    p.add_argument("--send", action="store_true", help="submit after simulation and preflight pass")
    p.set_defaults(fn=cmd_smoke)

    p = sub.add_parser("status", help="poll a submitted transaction")
    p.add_argument("--tx", required=True)
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except RealRefused as err:
        print(f"refused: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
