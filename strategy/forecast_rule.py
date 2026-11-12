"""Mel's forecast rule: the gross annual rate, as a fraction, that the agent
forecasts for a lending venue at a given horizon.

Written in cycle 1217 from strategy/playbook.md's forecast rules, the first
cycle after the operator's replay gate (core/replay.py) landed. The contract is
core/replay.py's: one module-level function, forecast(snapshot) -> float|None,
fed only what the row knew at entry. Live forecasts are this number unless the
note names a state change the rule cannot see.

What it encodes (playbook "What the evidence says today"):
- A vault forecast is the trailing 24-hour rate, at every horizon (cycle 525:
  the agent's vault error exceeded the trailing day's outside noise). Without
  one, the trailing week; without that, the advert times the vault haircut.
- An Arcadia tranche is never forecast above its trailing week (a00066).
- On Morpho the agent is level with the advertised rate, and the score's best
  Morpho baseline at 1 and 3 days is the advert. Morpho's advert is read from
  the IRM at entry, so it is the spot rate, not an event-window guess.
"""

VAULT_HAIRCUT = 0.85
ARCADIA_TRANCHES = {
    "0xa5e1e1f92a244f192943899eb5810e2bab372ea4",  # Arcadia Senior
}


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def forecast(snapshot: dict):
    kind = snapshot.get("kind")
    adv = _num(snapshot.get("advertised"))
    t24 = _num(snapshot.get("trailing_24h"))
    t7 = _num(snapshot.get("trailing_7d"))

    if kind == "erc4626_usdg_vault":
        if t24 is not None:
            rate = t24
        elif t7 is not None:
            rate = t7
        elif adv is not None:
            rate = adv * VAULT_HAIRCUT
        else:
            return None
        venue = (snapshot.get("venue") or "").lower()
        if venue in ARCADIA_TRANCHES and t7 is not None:
            rate = min(rate, t7)
        return max(rate, 0.0)

    if kind == "morpho_market":
        if adv is not None:
            return max(adv, 0.0)
        return t24 if t24 is not None else t7

    return None
