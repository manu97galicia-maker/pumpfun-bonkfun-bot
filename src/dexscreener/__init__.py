"""DexScreener API integration.

Thin async client over the public DexScreener HTTP API
(https://docs.dexscreener.com/api/reference), including the
"Dex Paid" check (paid token profile / boosts / ads orders).

No trading filter is wired here yet: this package only exposes the raw
endpoints plus a few convenience helpers so callers can decide how to use
the data.
"""

from dexscreener.client import (
    DexPaidStatus,
    DexScreenerClient,
    TokenMarketData,
)

__all__ = [
    "DexPaidStatus",
    "DexScreenerClient",
    "TokenMarketData",
]
