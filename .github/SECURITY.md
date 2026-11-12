# Security policy

## Reporting

Please use GitHub's private vulnerability reporting (Security tab, "Report a
vulnerability") rather than a public issue for anything exploitable. Reports
are read by the human operator, not by the agent.

Don't include private keys, tokens, or the contents of a Pearl Connect
`.mcp.json` in a report. Describe where they are exposed instead.

## Supported versions

Only `main` is supported. The repository has no releases; the agent commits
to `main` many times a day.

## Scope

The interesting surface is the money path and the boundaries around it:

- `core/real.py`, the only code that may touch real funds, and the caps in
  `config/protected.json` that it enforces.
- The protected-boundary enforcement: `loop.sh`'s revert list and the CI
  boundary guard (`.github/scripts/boundary.sh`).
- The integrity tripwires in `core/validate.py`, which replay every ledger
  row through the caps as of the moment it was written.
- `core/rpc.py`, the only code that reads the chain. The only other code
  that opens a connection is `core/real.py`, to the local Pearl Connect
  signer.

## Threat model, stated honestly

- The cycle agent has no web search and no web fetch. Its only network path
  is the RPC, through `core/rpc.py`. Chain data is still untrusted input:
  token names, venue metadata, and revert strings are written by whoever
  deployed the contract. Prompt injection through that data is the most
  interesting attack class.
- Real execution is dormant: `real_execution_enabled` is `false`, no venue
  is allowed for real use, and `loop.sh` refuses to cycle if
  `python3 core/real.py doctor` reports ready.
- When the operator turns real execution on, exposure is capped in three
  independent places: per-allocation, daily, and open-position caps in
  `config/protected.json` (with hard ceilings enforced by CI), the service
  safe's balance (the operator funds it deliberately small, and no code can
  spend past it), and the Pearl Connect signer's own guardrail, which runs
  outside the agent session.
- Keys never enter any agent session. Signing happens in Pearl Connect's
  local service; the session only names actions.
- The journal is public by design. Contract and wallet addresses appear
  there; tokens and credentials must not.

A report that moves money past those bounds, or gets an agent commit past
the protected-file boundary without an `operator:` prefix, is exactly what
this policy is for.

## Verify a fork before you run it

A fork of an agent repository is an easy place to hide code that runs on
your machine with your Claude account and your keys. Before you run anyone's
copy of Mel, including this one:

- Read the diff of `loop.sh`, `core/`, and `config/protected.json` against
  this repository's `main`.
- Check that `real_execution_enabled` is `false` and that
  `python3 core/real.py doctor` reports not ready.
- Check that `core/rpc.py` and `core/real.py` are still the only code that
  opens a network connection, and that `MEL_RPC_URL` points at an endpoint
  you chose.
- Don't set `PEARL_CONNECT_STORE` or fund a service safe for a fork you
  haven't read.
