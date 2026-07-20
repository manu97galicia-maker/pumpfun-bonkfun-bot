"""Pure logic for a tiered take-profit ladder.

A tier is ``(gain, sell)`` where ``gain`` is the fraction above entry that
triggers it (x2 = 1.0, x5 = 4.0) and ``sell`` is the fraction of the ORIGINAL
position to sell when it triggers. Whatever is left after all tiers fire is
held (moonbag). Stop-loss handling lives in the trader; this module only
decides which take-profit tiers are due at a given price.

Kept dependency-free so it can be unit-tested without RPC or a wallet.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tier:
    """One take-profit rung."""

    gain: float  # fraction above entry that triggers the sell (0.4 = +40%)
    sell: float  # fraction of the ORIGINAL quantity to sell


def build_tiers(pairs: list[tuple[float | None, float | None]]) -> list[Tier]:
    """Build a validated, ascending list of tiers from (gain, sell) pairs.

    Drops pairs with a missing/zero sell fraction, clamps sell to (0, 1],
    and sorts by trigger gain so lower targets fire first.

    Args:
        pairs: ``[(gain, sell), ...]`` possibly containing ``None``/zero.

    Returns:
        Cleaned tiers sorted by ascending gain.
    """
    tiers: list[Tier] = []
    for gain, sell in pairs:
        if gain is None or sell is None:
            continue
        if gain < 0 or sell <= 0:
            continue
        tiers.append(Tier(gain=float(gain), sell=min(float(sell), 1.0)))
    return sorted(tiers, key=lambda t: t.gain)


def tiers_due(
    entry_price: float,
    current_price: float,
    tiers: list[Tier],
    executed: list[bool],
) -> list[int]:
    """Return indices of tiers whose target price is reached and not yet sold.

    Args:
        entry_price: Position entry price (SOL per token).
        current_price: Current price (SOL per token).
        tiers: Tiers from :func:`build_tiers`.
        executed: Parallel list; True where a tier already sold.

    Returns:
        Indices (into ``tiers``) to execute now, in ascending order.
    """
    if entry_price <= 0:
        return []
    due: list[int] = []
    for i, tier in enumerate(tiers):
        if executed[i]:
            continue
        if current_price >= entry_price * (1.0 + tier.gain):
            due.append(i)
    return due


def moonbag_fraction(tiers: list[Tier]) -> float:
    """Fraction of the original position left held after all tiers fire."""
    return max(0.0, 1.0 - sum(t.sell for t in tiers))
