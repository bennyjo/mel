# Robinhood Chain facts

Operator-owned. Every line here was verified against the chain, not read from
a document. Date each entry. If the agent finds one of these wrong, it records
the correction in `journal/operator-notes.md` and asks; it does not edit this
file.

## Network

| Item | Value |
| --- | --- |
| Chain ID | 4663 (`0x1237`) |
| Type | Arbitrum Orbit L2, settles on Ethereum |
| Gas token | ETH, two-part fee covering execution and L1 data |
| Public RPC | `https://rpc.mainnet.chain.robinhood.com` |
| Explorer | `https://robinhoodchain.blockscout.com` |
| Sequencer | RHDA, LLC |

Verified 2026-09-08: `eth_chainId` returns `0x1237` and the head block was
near 58,030,000.

## Token and infrastructure addresses

| Contract | Address |
| --- | --- |
| USDG (Paxos) | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` |
| WETH | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` |
| Multicall (Robinhood) | `0x2cAC2D899eCC914d704FeaAE33ac1bF36277DaD1` |
| Safe L2 singleton 1.3.0 | `0x3E5c63644E683549055b9Be8653de26E0B4CD36E` |
| Safe proxy factory 1.3.0 | `0xa6B71E26C5e0845f74c812102Ca7114b6a896AB2` |
| MultiSend 1.3.0 | `0xA238CBeb142c10Ef7Ad8442C6D1f9E89e07e7761` |
| ERC-8004 Identity Registry | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |

Verified 2026-09-08 by `eth_getCode`: USDG, WETH, Permit2, and the Multicall
all return non-empty bytecode. USDG reports symbol `USDG`, 6 decimals, and a
total supply near 699,432,640 USDG.

The Safe, MultiSend, and registry addresses are recorded from published
deployment lists and are not yet confirmed by `eth_getCode`. Confirm before
relying on any of them.

## Venues

Confirmed venues are recorded in `journal/venues.jsonl`, one row per venue with
the address, the kind, the block it was confirmed at, and how. The depth, cost,
and yield figures that justified building on them are in
`journal/recon-2026-09-08.md`. Do not duplicate either here.

## Constraints that shape the engine

- **The public RPC rate-limits aggressively.** It answers with HTTP `200` and
  a JSON-RPC body of `{"error":{"code":429,"message":"Too Many Requests"}}`.
  Treat that body as a retryable failure, not as data. Verified 2026-09-08.
- **The public RPC rejects some clients by User-Agent.** A default Python
  `urllib` request returns HTTP `403 Forbidden`. A browser User-Agent
  succeeds. Verified 2026-09-08.
- **JSON-RPC batching works** and is the main way to stay under the rate
  limit. A two-call batch returned both results. Verified 2026-09-08.
- **The explorer API is not scriptable.** `robinhoodchain.blockscout.com`
  serves a Cloudflare interstitial to command-line clients. Read chain state
  over RPC. Verified 2026-09-08.
- **The operator's runs read the chain through the Olas RPC gateway**,
  the endpoint Pearl configures for Connect services on chain 4663, set
  through `MEL_RPC_URL`. Verified 2026-10-06: a full cost quote ran with zero
  throttling, and state 20,000 blocks old was still readable. The default in
  `config/protected.json` is the public endpoint, which the facts that
  follow describe.
- **The public RPC is not an archive node.** State is served for about
  6,100 blocks, roughly ten minutes; older blocks answer `-32000 metadata is
  not found`. Record entry state at entry and read exit state live, because
  neither can be re-read later. Found 2026-09-09.
- **Batch limit is 60 calls per request**, regardless of payload size; 70 is
  refused. `eth_getLogs` returns at most 10,000 rows per query. Found
  2026-09-09.
- **Users transact through ERC-4337.** Sampled Morpho supplies and vault
  deposits arrive through EntryPoint v0.7 at
  `0x0000000071727de22e5e9d8baf0edac6f37da032`, about 475,000 gas per
  UserOp. Receipt-derived gas measures the smart account, not the action.
  Found 2026-09-09.
- **Pearl Connect runs on 4663 (since 2026-10-06).** The operator's Connect
  service is #12 on the chain's service registry, with service safe
  `0xb1d34E2884Fdc4813339e0BD0d123Ea92b5EF3AB`. Its session brief says mechs
  on this chain are paid in USDG. Connect's `preflight_transaction` checks the
  guardrail policy only; it does not simulate. Mel simulates with `eth_call`
  from the safe.
- **There is no on-chain prediction market on 4663.** This chain is a DeFi
  venue, which is why Mel allocates rather than forecasts events.
- **The endpoint serves state for about 6,100 blocks, roughly ten minutes.**
  An `eth_call` at an older block returns `-32000 metadata is not found,
  <block>`. This is not an archive node, at any depth. Anything the engine will
  need later must be recorded when it is read. Verified 2026-09-08 by binary
  search from the head; see `journal/recon-2026-09-08.md`.
- **The JSON-RPC batch limit counts calls, not bytes.** A batch of 60
  `eth_call` is accepted, 70 is refused, and 100 is refused even when every
  call is a bare `decimals()`. `core/rpc.py` batches 50. Verified 2026-09-08.
- **`eth_getLogs` caps a query at 10,000 matched logs** and separately times
  out queries that scan too far, both reported as `-32000`. Size a window by
  expected match density, not by block count. Verified 2026-09-08.
- **Real users transact through ERC-4337.** Every recent Morpho supply and
  ERC-4626 vault deposit sampled arrived through EntryPoint v0.7 at
  `0x0000000071727de22e5e9d8baf0edac6f37da032` and cost about 475,000 gas for
  the whole UserOp. Gas read from a receipt therefore measures the
  smart-account bundle, not the action; use `eth_estimateGas` from a direct
  caller instead. Verified 2026-09-08.
- **An ERC-4626 rate is not readable from state on this endpoint.** Share
  prices carry six decimals and state survives ten minutes, so the smallest
  visible move over a served window is about 4% APY and the usual reading is
  exactly zero. The rate comes from the vault's own `Deposit` and `Withdraw`
  events instead, whose `assets` and `shares` fields pin the share price at any
  past block: a transfer of 1,000 USDG gives nine significant digits against
  the six `convertToAssets` offers. Logs are retained to genesis, state is not.
  Verified 2026-09-09; `core/venues.py` reads vault rates this way.
- **A venue can hold real money and still have no readable rate.** Steakhouse
  Turbo USDG (`0xbeefff136e3684273e6aa75a1669b784b373a4fd`) holds 8.08M USDG
  and has emitted two ERC-4626 events in six million blocks, both for 1 USDG.
  The engine reports no rate for it rather than inventing one. Verified
  2026-09-09.
- **Morpho's stored market state only moves when the market is touched.** At
  blocks 58,093,251 and 58,099,049 - 586 seconds apart - the deepest quoting
  USDG market (`0xaa586d26a6fe...`, 360k USDG supplied, 63.4% advertised) held
  identical `totalSupplyAssets`, `totalSupplyShares` and `lastUpdate`, so its
  share price read flat while interest was accruing the whole time. A real
  `withdraw` calls `_accrueInterest` first, so a realized return taken from the
  stored share price alone reports zero and is wrong. `core/settle.py` replays
  Morpho's own accrual - Taylor-compounded borrow rate, fee shares minted -
  before taking the ratio, and records the IRM's per-second rate on the row at
  entry because it cannot be re-read later. Replayed over seven days that gives
  63.05% against the 63.41% the IRM advertised, two numbers computed by paths
  that never meet. Verified 2026-09-09.
- **The sequencer screens transactions.** A screened transaction is dropped
  without a revert and without an event. A submitted transaction that never
  appears is a distinct outcome from a failed one, and the real-execution
  path must tell them apart.
