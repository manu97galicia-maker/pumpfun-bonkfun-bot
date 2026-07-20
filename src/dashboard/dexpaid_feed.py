"""Live feed of Solana tokens that recently paid DexScreener (dex paid / boosts).

Powers the dashboard's "Pagaron Dex" panel and is the detection source for the
buy-on-dex-paid strategy. Measured reality: a paid order is visible on the API
minutes after payment (not seconds), so this polls the latest-boosts feed and
surfaces recently-paid tokens with the data needed to decide a buy:
boost amount, payment age, price/liquidity, and RugCheck locked-liquidity %.

Bounded on purpose (a handful of tokens per refresh) to stay within the 60/300
rpm DexScreener limits and RugCheck's gentle limit.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from dexscreener.client import DexScreenerClient
from dexscreener.rugcheck import RugCheckClient
from utils.logger import get_logger

logger = get_logger(__name__)


async def get_candidates(
    limit: int = 10,
    max_age_minutes: float = 120.0,
    chain: str = "solana",
    check_locked: bool = True,
) -> list[dict[str, Any]]:
    """Return recently dex-paid tokens with buy-decision data.

    Args:
        limit: Max tokens to return (kept small for rate limits).
        max_age_minutes: Only include tokens whose newest approved paid order
            is younger than this.
        chain: Chain slug.
        check_locked: Also query RugCheck for locked-liquidity % (slower).

    Returns:
        List of dicts sorted by most-recent payment first, each with mint,
        symbol, boosts, payment_age_min, price_usd, liquidity_usd, dex_id,
        migrated flag, and (if check_locked) lp_locked_pct.
    """
    now_ms = time.time() * 1000.0
    out: list[dict[str, Any]] = []

    async with DexScreenerClient(chain=chain) as dex:
        boosts = await dex.get_latest_boosts()
        toks = [b for b in boosts if b.get("chainId") == chain][: limit * 3]

        async def build(entry: dict[str, Any]) -> dict[str, Any] | None:
            mint = entry.get("tokenAddress")
            if not mint:
                return None
            orders = await dex.get_orders(mint, chain)
            paid_ts = [
                o.get("paymentTimestamp")
                for o in orders
                if o.get("status") == "approved" and o.get("paymentTimestamp")
            ]
            if not paid_ts:
                return None
            age_min = (now_ms - max(paid_ts)) / 60000.0
            if age_min > max_age_minutes:
                return None
            market = await dex.get_market_data(mint, chain)
            dex_id = market.dex_id if market else None
            migrated = dex_id in ("pumpswap", "raydium") if dex_id else None
            return {
                "mint": mint,
                "symbol": (market.raw.get("baseToken", {}).get("symbol") if market else None)
                or "?",
                "boosts": entry.get("totalAmount") or entry.get("amount") or 0,
                "payment_age_min": round(age_min, 1),
                "price_usd": market.price_usd if market else None,
                "liquidity_usd": market.liquidity_usd if market else None,
                "dex_id": dex_id,
                "migrated": migrated,
            }

        built = await asyncio.gather(*[build(t) for t in toks])
        rows = [r for r in built if r][:limit]

        if check_locked and rows:
            async with RugCheckClient() as rc:
                reports = await asyncio.gather(
                    *[rc.get_report(r["mint"]) for r in rows]
                )
                for r, rep in zip(rows, reports):
                    r["lp_locked_pct"] = rep.lp_locked_pct if rep.found else None
                    r["rugged"] = rep.rugged if rep.found else None

    rows.sort(key=lambda r: r["payment_age_min"])
    return rows
