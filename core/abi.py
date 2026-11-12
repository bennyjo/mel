"""ABI encoding, decoding, and the selector/topic table.

PROTECTED ENGINE FILE. The allocating agent may not edit this file. Only the
human operator changes it. `loop.sh` reverts an agent change here and CI fails
any agent commit that touches `core/`.

Owns:
  - Keccak-256 (Ethereum's, not NIST SHA3-256; the padding byte differs).
  - The one table of function selectors and event topics used anywhere in the
    engine, with the human signature next to each hex value.
  - Minimal hand-rolled ABI encoding and decoding for static types plus the
    two dynamic types the engine reads (`string`, `bytes`).

There is no strategy judgment in this file and there must never be. Run
`python3 core/abi.py` to recompute every hex value in the table from its
signature and fail loudly on a mismatch.
"""

from __future__ import annotations

import decimal

# --------------------------------------------------------------------------
# Keccak-256
#
# hashlib.sha3_256 is NIST SHA-3, which pads with 0x06. Ethereum uses the
# original Keccak submission, which pads with 0x01. They are different hashes,
# so the permutation is implemented here rather than borrowed from hashlib.
# --------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

_ROTATION = (1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14,
             27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44)

_PI_LANE = (10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4,
            15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1)

_RATE_BYTES = 136  # 1600 bits of state - 2 * 256 bits of capacity


def _rotl64(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _keccak_f1600(lanes: list[int]) -> None:
    for rnd in range(24):
        # theta
        column = [lanes[x] ^ lanes[x + 5] ^ lanes[x + 10] ^ lanes[x + 15] ^ lanes[x + 20]
                  for x in range(5)]
        for x in range(5):
            d = column[(x + 4) % 5] ^ _rotl64(column[(x + 1) % 5], 1)
            for y in range(0, 25, 5):
                lanes[y + x] ^= d
        # rho and pi
        carry = lanes[1]
        for i in range(24):
            target = _PI_LANE[i]
            held = lanes[target]
            lanes[target] = _rotl64(carry, _ROTATION[i])
            carry = held
        # chi
        for y in range(0, 25, 5):
            row = lanes[y:y + 5]
            for x in range(5):
                lanes[y + x] = row[x] ^ ((~row[(x + 1) % 5] & _MASK64) & row[(x + 2) % 5])
        # iota
        lanes[0] ^= _ROUND_CONSTANTS[rnd]


def keccak256(data: bytes) -> bytes:
    """Ethereum's Keccak-256 of `data`."""
    lanes = [0] * 25
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE_BYTES != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    for offset in range(0, len(padded), _RATE_BYTES):
        block = padded[offset:offset + _RATE_BYTES]
        for i in range(_RATE_BYTES // 8):
            lanes[i] ^= int.from_bytes(block[i * 8:i * 8 + 8], "little")
        _keccak_f1600(lanes)

    out = bytearray()
    for i in range(4):  # 32 bytes of squeeze, all from the first rate block
        out += lanes[i].to_bytes(8, "little")
    return bytes(out)


def selector(signature: str) -> str:
    """First four bytes of keccak256(signature), as `0x`-prefixed hex."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def topic(signature: str) -> str:
    """Full 32-byte keccak256(signature), as `0x`-prefixed hex (an event topic0)."""
    return "0x" + keccak256(signature.encode()).hex()


# --------------------------------------------------------------------------
# The table. Human signature -> hex value. `python3 core/abi.py` recomputes
# every entry and fails on a mismatch, so the literals here stay honest.
# --------------------------------------------------------------------------

SELECTORS: dict[str, str] = {
    # ERC-20
    "name()": "0x06fdde03",
    "symbol()": "0x95d89b41",
    "decimals()": "0x313ce567",
    "totalSupply()": "0x18160ddd",
    "balanceOf(address)": "0x70a08231",
    "allowance(address,address)": "0xdd62ed3e",
    "approve(address,uint256)": "0x095ea7b3",
    "transfer(address,uint256)": "0xa9059cbb",

    # Uniswap v2 pair and factory
    "token0()": "0x0dfe1681",
    "token1()": "0xd21220a7",
    "getReserves()": "0x0902f1ac",
    "factory()": "0xc45a0155",
    "allPairsLength()": "0x574f2ba3",
    "allPairs(uint256)": "0x1e3dd18b",
    "getPair(address,address)": "0xe6a43905",
    "feeTo()": "0x017e7e58",

    # Uniswap v3 pool and factory
    "slot0()": "0x3850c7bd",
    "liquidity()": "0x1a686502",
    "fee()": "0xddca3f43",
    "tickSpacing()": "0xd0c93a7c",
    "feeGrowthGlobal0X128()": "0xf3058399",
    "feeGrowthGlobal1X128()": "0x46141319",
    "getPool(address,address,uint24)": "0x1698ee82",
    "feeAmountTickSpacing(uint24)": "0x22afcccb",
    "ticks(int24)": "0xf30dba93",
    "positions(bytes32)": "0x514ea4bf",

    # Uniswap v4 PoolManager (state is read through the state-view/extsload path)
    "extsload(bytes32)": "0x1e2eaeaf",

    # ERC-4626, which covers MetaMorpho vaults
    "asset()": "0x38d52e0f",
    "totalAssets()": "0x01e1d114",
    "convertToAssets(uint256)": "0x07a2d13a",
    "convertToShares(uint256)": "0xc6e6f592",
    "previewDeposit(uint256)": "0xef8b30f7",
    "previewRedeem(uint256)": "0x4cdad506",
    "maxDeposit(address)": "0x402d267d",

    # Morpho Blue
    "market(bytes32)": "0x5c60e39a",
    "idToMarketParams(bytes32)": "0x2c3c9157",
    "position(bytes32,address)": "0x93c52062",
    "owner()": "0x8da5cb5b",

    # The only two write selectors in this table. Nothing in the engine calls
    # them: `core/real.py` builds the calldata and is dormant, and every read
    # path above is a view. They are listed here anyway so the calldata a
    # future real allocation would sign is checked by `check_table()` like
    # everything else, rather than computed ad hoc at the moment it matters.
    "supply((address,address,address,address,uint256),uint256,uint256,address,bytes)":
        "0xa99aad89",
    "deposit(uint256,address)": "0x6e553f65",

    # MetaMorpho / Morpho IRM
    "borrowRateView((address,address,address,address,uint256),(uint128,uint128,uint128,uint128,uint128,uint128))":
        "0x8c00bf6b",
}

TOPICS: dict[str, str] = {
    "Transfer(address,address,uint256)":
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    "Approval(address,address,uint256)":
        "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",
    # Uniswap v2
    "PairCreated(address,address,address,uint256)":
        "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9",
    "Sync(uint112,uint112)":
        "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1",
    # Uniswap v3
    "PoolCreated(address,address,uint24,int24,address)":
        "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118",
    "Swap(address,address,int256,int256,uint160,uint128,int24)":
        "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
    # Uniswap v4
    "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)":
        "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438",
    # Morpho Blue
    "CreateMarket(bytes32,(address,address,address,address,uint256))":
        "0xac4b2400f169220b0c0afdde7a0b32e775ba727ea1cb30b35f935cdaab8683ac",
    "Supply(bytes32,address,address,uint256,uint256)":
        "0xedf8870433c83823eb071d3df1caa8d008f12f6440918c20d75a3602cda30fe0",
    "Withdraw(bytes32,address,address,address,uint256,uint256)":
        "0xa56fc0ad5702ec05ce63666221f796fb62437c32db1aa1aa075fc6484cf58fbf",
    "CreateMetaMorpho(address,address,address,uint256,address,string,string,bytes32)":
        "0xed8c95d05909b0f217f3e68171ef917df4b278d5addfe4dda888e90279be7d1d",
    # ERC-4626
    "Deposit(address,address,uint256,uint256)":
        "0xdcbc1c05240f31ff3ad067ef1ee35ce4997762752e3a095284754544f4c709d7",
    "Withdraw(address,address,address,uint256,uint256)":
        "0xfbde797d201c681b91056529119e0b02407c7bb96a4a2c75c01fc9667232c8db",
}


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

class AbiError(Exception):
    """Raised on an ABI value that does not fit its declared type."""


def _strip0x(value: str) -> str:
    return value[2:] if value.startswith(("0x", "0X")) else value


def encode_address(value: str) -> bytes:
    raw = bytes.fromhex(_strip0x(value).rjust(40, "0"))
    if len(raw) != 20:
        raise AbiError(f"not a 20-byte address: {value}")
    return b"\x00" * 12 + raw


def encode_uint(value: int, bits: int = 256) -> bytes:
    if value < 0 or value >= (1 << bits):
        raise AbiError(f"uint{bits} out of range: {value}")
    return value.to_bytes(32, "big")


def encode_int(value: int, bits: int = 256) -> bytes:
    limit = 1 << (bits - 1)
    if value < -limit or value >= limit:
        raise AbiError(f"int{bits} out of range: {value}")
    return (value & ((1 << 256) - 1)).to_bytes(32, "big")


def encode_bytes32(value: str | bytes) -> bytes:
    raw = value if isinstance(value, bytes) else bytes.fromhex(_strip0x(value))
    if len(raw) != 32:
        raise AbiError(f"not 32 bytes: {value!r}")
    return raw


def _arg_types(signature: str) -> list[str]:
    inner = signature[signature.index("(") + 1:signature.rindex(")")]
    return [t for t in inner.split(",") if t]


def encode_call(signature: str, *args: object) -> str:
    """`0x`-prefixed calldata for `signature` applied to `args`.

    Static types only, which is every call the engine makes. The selector is
    taken from SELECTORS when the signature is listed there so a typo in the
    table cannot silently produce a different call.
    """
    sel = SELECTORS.get(signature) or selector(signature)
    types = _arg_types(signature)
    if len(types) != len(args):
        raise AbiError(f"{signature} takes {len(types)} args, got {len(args)}")
    body = b""
    for typ, arg in zip(types, args):
        if typ == "address":
            body += encode_address(str(arg))
        elif typ == "bytes32":
            body += encode_bytes32(arg)  # type: ignore[arg-type]
        elif typ == "bool":
            body += encode_uint(1 if arg else 0)
        elif typ.startswith("uint"):
            body += encode_uint(int(arg), int(typ[4:] or 256))  # type: ignore[arg-type]
        elif typ.startswith("int"):
            body += encode_int(int(arg), int(typ[3:] or 256))  # type: ignore[arg-type]
        else:
            raise AbiError(f"unsupported argument type {typ!r} in {signature}")
    return "0x" + sel[2:] + body.hex()


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def _words(data: str) -> list[bytes]:
    raw = bytes.fromhex(_strip0x(data))
    return [raw[i:i + 32] for i in range(0, len(raw) - len(raw) % 32, 32)]


def decode_uint(data: str, index: int = 0) -> int:
    words = _words(data)
    if index >= len(words):
        raise AbiError(f"no word {index} in {len(words)}-word return")
    return int.from_bytes(words[index], "big")


def decode_int(data: str, index: int = 0, bits: int = 256) -> int:
    value = decode_uint(data, index)
    limit = 1 << (bits - 1)
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >= limit else value


def decode_address(data: str, index: int = 0) -> str:
    return "0x" + _words(data)[index][12:].hex()


def decode_bytes32(data: str, index: int = 0) -> str:
    return "0x" + _words(data)[index].hex()


def decode_uints(data: str, count: int) -> list[int]:
    words = _words(data)
    if len(words) < count:
        raise AbiError(f"expected {count} words, got {len(words)}")
    return [int.from_bytes(w, "big") for w in words[:count]]


def decode_string(data: str) -> str:
    """Decode a `string` return, tolerating the old bytes32 convention."""
    raw = bytes.fromhex(_strip0x(data))
    if len(raw) == 32:  # non-standard bytes32 symbol/name
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    if len(raw) < 64:
        return ""
    offset = int.from_bytes(raw[0:32], "big")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    return raw[offset + 32:offset + 32 + length].decode("utf-8", "replace")


def topic_to_address(topic_hex: str) -> str:
    return "0x" + _strip0x(topic_hex)[-40:]


# --------------------------------------------------------------------------
# Fixed-point amounts
#
# Every token amount in a journal row is a decimal string in whole units, never
# a raw integer and never a float. These two functions are the only sanctioned
# conversion between the chain's integers and that representation, and neither
# of them goes through `float`.
# --------------------------------------------------------------------------

def fixed_to_str(raw: int, decimals: int) -> str:
    """A raw on-chain integer as an exact decimal string in whole units."""
    sign = "-" if raw < 0 else ""
    raw = abs(int(raw))
    if decimals == 0:
        return sign + str(raw)
    whole, frac = divmod(raw, 10 ** decimals)
    return f"{sign}{whole}.{frac:0{decimals}d}"


def str_to_fixed(value: str | int, decimals: int) -> int:
    """The inverse of `fixed_to_str`. Refuses more precision than `decimals`."""
    scaled = decimal.Decimal(str(value)).scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise AbiError(f"{value!r} carries more than {decimals} decimals")
    return int(scaled)


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------

def check_table() -> list[str]:
    """Recompute every table entry from its signature. Returns the mismatches."""
    problems = []
    for sig, want in SELECTORS.items():
        got = selector(sig)
        if got != want:
            problems.append(f"SELECTORS[{sig!r}] = {want}, recomputed {got}")
    for sig, want in TOPICS.items():
        got = topic(sig)
        if got != want:
            problems.append(f"TOPICS[{sig!r}] = {want}, recomputed {got}")
    return problems


def _self_test() -> None:
    empty = keccak256(b"").hex()
    assert empty == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470", empty
    abc = keccak256(b"abc").hex()
    assert abc == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45", abc
    long = keccak256(b"a" * 200).hex()  # exercises multi-block absorb
    assert len(long) == 64
    assert selector("transfer(address,uint256)") == "0xa9059cbb"
    call = encode_call("balanceOf(address)", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")
    assert call == ("0x70a08231000000000000000000000000"
                    "5fc5360d0400a0fd4f2af552add042d716f1d168"), call
    assert decode_int("0x" + "f" * 64, 0, 24) == -1
    assert decode_address("0x" + "00" * 12 + "11" * 20) == "0x" + "11" * 20


if __name__ == "__main__":
    import sys

    _self_test()
    bad = check_table()
    for line in bad:
        print("MISMATCH:", line)
    print(f"{len(SELECTORS)} selectors, {len(TOPICS)} topics, {len(bad)} mismatches")
    sys.exit(1 if bad else 0)
