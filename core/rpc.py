"""The only network path in Mel: a batching JSON-RPC client for chain 4663.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

Owns:
  - the endpoint, read from `config/protected.json` and overridden by the
    `MEL_RPC_URL` environment variable;
  - every HTTP request the engine makes. No other module opens a socket, and
    nothing in this repo talks to anything but the RPC. The block explorer is
    behind Cloudflare and is not a data source;
  - the rate-limit discipline the public endpoint demands: a browser
    User-Agent (a default urllib agent gets 403), one request in flight at a
    time, JSON-RPC batching, and exponential backoff with jitter when the
    endpoint answers HTTP 200 with a JSON-RPC `429` body;
  - the per-run request count, appended to `journal/rpc-usage.jsonl` so the
    operator can size a paid endpoint from evidence;
  - `forbid_network`, which turns "this command is offline" from a claim into
    a property the process enforces. `core/validate.py` and `core/score.py
    --offline` call it, so an edit that adds a chain read to either fails
    rather than making CI depend on an endpoint.

There is no strategy judgment in this file and there must never be.

Usage:
    from core import rpc
    head = rpc.block_number()
    sym  = rpc.eth_call(TOKEN, abi.encode_call("symbol()"))
    outs = rpc.batch([("eth_getBlockByNumber", [hex(n), False]) for n in blocks])
"""

from __future__ import annotations

import atexit
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "protected.json"
USAGE_LOG = ROOT / "journal" / "rpc-usage.jsonl"

# A default urllib User-Agent is answered with HTTP 403 by the public
# endpoint. Verified 2026-09-08; see CHAIN.md.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MAX_BATCH = 50          # calls per HTTP request. The public endpoint refuses a batch
                        # of 70 eth_call and accepts 60, and refuses 100 even when every
                        # call is a bare decimals(), so the cap counts calls, not bytes.
                        # Verified 2026-09-08; 50 leaves headroom for a heavier method.
MAX_ATTEMPTS = 9        # per HTTP request, before giving up. Seven attempts is
                        # about 50s of backoff, and on 2026-09-09 a wide
                        # eth_getLogs scan followed by an eth_call batch was
                        # throttled for longer than that. Nine is about 200s,
                        # which an hourly cycle can afford.
BASE_BACKOFF = 0.8      # seconds, doubled each attempt
MAX_BACKOFF = 32.0
TIMEOUT = 45.0

MIN_GAP_START = 0.12    # seconds between requests, grows when throttled
MIN_GAP_MAX = 6.0
GAP_GROWTH = 1.6        # on a 429
GAP_DECAY = 0.93        # per clean request


class RpcError(Exception):
    """A JSON-RPC error the client will not retry."""


class RateLimited(Exception):
    """The endpoint said 429, in a header or in the JSON body."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

_config_cache: dict | None = None


def protected_config() -> dict:
    """The parsed `config/protected.json`. Cached; never mutated in place."""
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            _config_cache = json.load(handle)
    return _config_cache


def endpoint() -> str:
    """The RPC URL. `MEL_RPC_URL` wins so a paid endpoint drops in unchanged."""
    override = os.environ.get("MEL_RPC_URL", "").strip()
    return override or protected_config()["rpc_url"]


def endpoint_label() -> str:
    """What the usage journal records in place of the URL: "public" for the
    config default, "custom" for anything else. A paid endpoint's URL often
    carries its API key, and the journal is committed."""
    return "public" if endpoint() == protected_config()["rpc_url"] else "custom"


# --------------------------------------------------------------------------
# Request accounting
# --------------------------------------------------------------------------

class _Stats:
    def __init__(self) -> None:
        self.http_requests = 0
        self.rpc_calls = 0
        self.retries = 0
        self.rate_limited = 0
        self.errors = 0
        self.wall_seconds = 0.0
        self.started = time.time()

    def as_dict(self) -> dict:
        return {
            "http_requests": self.http_requests,
            "rpc_calls": self.rpc_calls,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "errors": self.errors,
            "wall_seconds": round(self.wall_seconds, 2),
        }


STATS = _Stats()

_lock = threading.Lock()      # caps concurrent requests at one, by construction
_last_request_at = 0.0
_min_gap = MIN_GAP_START


def stats() -> dict:
    """Request counters for this process."""
    return STATS.as_dict()


def _append_usage() -> None:
    """One row per run into journal/rpc-usage.jsonl. Registered with atexit so
    the count is recorded even when a cycle aborts."""
    if STATS.http_requests == 0:
        return
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "context": os.environ.get("MEL_CONTEXT", "adhoc"),
        "endpoint": endpoint_label(),
        "min_gap_end": round(_min_gap, 3),
        **STATS.as_dict(),
    }
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass  # accounting must never break a cycle


atexit.register(_append_usage)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def _is_rate_limit(payload: object) -> bool:
    """True when a JSON-RPC body carries a 429. The endpoint returns HTTP 200
    with `{"error":{"code":429,...}}`, so this must be treated as a failure,
    not as data. Verified 2026-09-08; see CHAIN.md."""
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        err = item.get("error")
        if not isinstance(err, dict):
            continue
        if err.get("code") == 429:
            return True
        message = str(err.get("message", "")).lower()
        if "too many requests" in message or "rate limit" in message:
            return True
    return False


_forbidden: str | None = None


def forbid_network(reason: str = "this command is declared offline") -> None:
    """Make any further request raise. `core/score.py --offline` and
    `core/validate.py` call this so that "offline" is a property the process
    enforces rather than a claim the docstring makes: an edit that adds a
    chain read to either of them fails CI instead of quietly making the report
    depend on an endpoint."""
    global _forbidden
    _forbidden = reason


def _post(body: object) -> object:
    """One HTTP POST, with the pacing and backoff the public endpoint needs."""
    global _last_request_at, _min_gap
    if _forbidden:
        raise RpcError(f"network use is forbidden here: {_forbidden}")

    data = json.dumps(body).encode()
    request = urllib.request.Request(
        endpoint(),
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    delay = BASE_BACKOFF
    last_error: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        with _lock:  # one request in flight, always
            gap = _min_gap - (time.time() - _last_request_at)
            if gap > 0:
                time.sleep(gap)
            start = time.time()
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    raw = response.read()
                status = 200
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                status = exc.code
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raw, status, last_error = b"", 0, exc
            finally:
                _last_request_at = time.time()
                STATS.http_requests += 1
                STATS.wall_seconds += time.time() - start

        if status == 403:
            raise RpcError(
                "endpoint returned 403; the User-Agent was rejected. "
                "core/rpc.py sends a browser agent, so this is a new behaviour."
            )

        payload: object = None
        if status and raw:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None  # Cloudflare HTML or a truncated body: retry

        throttled = status == 429 or (payload is not None and _is_rate_limit(payload))
        if throttled:
            STATS.rate_limited += 1
            _min_gap = min(MIN_GAP_MAX, _min_gap * GAP_GROWTH)
            # Recorded so the give-up message names the cause. Without this the
            # operator sees "gave up after N attempts: None".
            last_error = RateLimited(
                f"throttled on attempt {attempt + 1}; pacing now {_min_gap:.2f}s")
        elif payload is not None and status == 200:
            _min_gap = max(MIN_GAP_START, _min_gap * GAP_DECAY)
            return payload
        else:
            last_error = last_error or RpcError(f"HTTP {status}: {raw[:200]!r}")

        STATS.retries += 1
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(min(MAX_BACKOFF, delay) * random.uniform(0.7, 1.3))
            delay *= 2

    STATS.errors += 1
    raise RpcError(f"gave up after {MAX_ATTEMPTS} attempts: {last_error}")


def _unwrap(item: dict, method: str, params: list) -> object:
    if "error" in item and item["error"] is not None:
        raise RpcError(f"{method}{params}: {item['error']}")
    return item.get("result")


def call(method: str, params: list | None = None) -> object:
    """A single JSON-RPC call. Prefer `batch` wherever more than one is needed."""
    params = params or []
    STATS.rpc_calls += 1
    payload = _post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if isinstance(payload, list):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RpcError(f"{method}: unexpected response {payload!r}")
    return _unwrap(payload, method, params)


def batch(calls: list[tuple[str, list]], allow_errors: bool = False,
          chunk: int = MAX_BATCH) -> list:
    """Run `calls` as JSON-RPC batches and return results in the same order.

    Batching is the main lever against the rate limit: one HTTP request buys
    up to `chunk` calls. With `allow_errors`, a failing call yields an
    `RpcError` instance in its slot instead of raising.
    """
    results: list = [None] * len(calls)
    for start in range(0, len(calls), chunk):
        window = calls[start:start + chunk]
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                for i, (m, p) in enumerate(window)]
        STATS.rpc_calls += len(window)
        payload = _post(body)
        if isinstance(payload, dict):  # a whole-batch error slipped through
            raise RpcError(f"batch failed: {payload.get('error', payload)}")
        by_id = {item.get("id"): item for item in payload if isinstance(item, dict)}
        for i, (method, params) in enumerate(window):
            item = by_id.get(i)
            if item is None:
                raise RpcError(f"batch response missing id {i} for {method}")
            try:
                results[start + i] = _unwrap(item, method, params)
            except RpcError as exc:
                if not allow_errors:
                    raise
                results[start + i] = exc
    return results


# --------------------------------------------------------------------------
# Typed conveniences. Everything else in core/ goes through these.
# --------------------------------------------------------------------------

def to_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    raise RpcError(f"not a quantity: {value!r}")


def chain_id() -> int:
    return to_int(call("eth_chainId"))


def block_number() -> int:
    return to_int(call("eth_blockNumber"))


def gas_price() -> int:
    """Wei. On this Orbit chain the fee is two-part; `eth_gasPrice` is the
    execution leg and the L1 data leg lands in the receipt's gas used."""
    return to_int(call("eth_gasPrice"))


def get_block(number: int | str, full: bool = False) -> dict:
    tag = number if isinstance(number, str) else hex(number)
    result = call("eth_getBlockByNumber", [tag, full])
    if not isinstance(result, dict):
        raise RpcError(f"no block {tag}")
    return result


def get_code(address: str, block: str = "latest") -> str:
    return str(call("eth_getCode", [address, block]))


def eth_call(to: str, data: str, block: int | str = "latest",
             from_: str | None = None) -> str:
    tag = block if isinstance(block, str) else hex(block)
    obj = {"to": to, "data": data}
    if from_:
        obj["from"] = from_
    return str(call("eth_call", [obj, tag]))


def eth_call_batch(calls: list[tuple[str, str]], block: int | str = "latest",
                   allow_errors: bool = True) -> list:
    """(to, data) pairs in one or more batches. Reverts come back as RpcError
    objects by default, because probing an address for a selector it does not
    implement is a normal part of venue discovery."""
    tag = block if isinstance(block, str) else hex(block)
    return batch([("eth_call", [{"to": to, "data": data}, tag]) for to, data in calls],
                 allow_errors=allow_errors)


def get_logs(from_block: int, to_block: int, address: str | list[str] | None = None,
             topics: list | None = None) -> list[dict]:
    """One `eth_getLogs`. Use `scan_logs` for ranges that may exceed the
    endpoint's window."""
    params: dict = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
    if address:
        params["address"] = address
    if topics:
        params["topics"] = topics
    result = call("eth_getLogs", [params])
    return list(result) if isinstance(result, list) else []


def scan_logs(from_block: int, to_block: int, address: str | list[str] | None = None,
              topics: list | None = None, span: int = 500_000,
              max_batch: int = 25, on_progress=None) -> list[dict]:
    """`eth_getLogs` over a wide range, batched, subdividing what the endpoint
    refuses.

    The public endpoint enforces two limits that a caller cannot know in
    advance: a hard cap of 10,000 matched logs per query, and a server-side
    timeout on queries that scan too many blocks. Both surface as JSON-RPC
    -32000. So windows are asked for in batches, and any window that comes
    back as an error is split in half and re-asked. A dense topic converges to
    small windows; a rare one is answered in a single pass over the chain.

    Returns logs sorted by (blockNumber, logIndex).
    """
    pending: list[tuple[int, int]] = [
        (s, min(to_block, s + span - 1)) for s in range(from_block, to_block + 1, span)
    ]
    found: list[dict] = []
    while pending:
        window = pending[:max_batch]
        pending = pending[max_batch:]
        params = []
        for lo, hi in window:
            item: dict = {"fromBlock": hex(lo), "toBlock": hex(hi)}
            if address:
                item["address"] = address
            if topics:
                item["topics"] = topics
            params.append(("eth_getLogs", [item]))
        results = batch(params, allow_errors=True, chunk=max_batch)
        split: list[tuple[int, int]] = []
        for (lo, hi), result in zip(window, results):
            if isinstance(result, RpcError):
                if lo >= hi:
                    raise RpcError(f"single block {lo} still refused: {result}")
                mid = (lo + hi) // 2
                split.extend([(lo, mid), (mid + 1, hi)])
            elif isinstance(result, list):
                found.extend(result)
        pending = split + pending
        if on_progress:
            on_progress(len(pending), len(found))
    found.sort(key=lambda lg: (to_int(lg["blockNumber"]), to_int(lg["logIndex"])))
    return found


if __name__ == "__main__":
    import sys

    cfg = protected_config()
    print(f"endpoint      {endpoint()}")
    head = block_number()
    cid = chain_id()
    price = gas_price()
    print(f"chain_id      {cid} (expected {cfg['chain_id']})")
    print(f"head block    {head:,}")
    print(f"gas price     {price:,} wei = {price / 1e9:.6f} gwei")
    print(f"requests      {json.dumps(stats(), sort_keys=True)}")
    sys.exit(0 if cid == cfg["chain_id"] else 1)
