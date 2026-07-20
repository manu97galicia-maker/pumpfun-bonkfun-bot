"""Acquisition source: buy tokens that recently paid DexScreener.

Polls the dex-paid feed, rebuilds a TokenInfo for each fresh, non-migrated
pump.fun candidate, and hands it to the trader's normal buy path (where the
DexScreener filter still applies). This is the "comprar al pagar dex" trigger.

Measured reality: a paid order is visible minutes after payment, so this polls
on an interval rather than reacting in milliseconds.

WARNING: the buy path for existing mints is UNVERIFIED against a live buy in
this workspace — enable with a tiny buy_amount and watch the logs first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from interfaces.core import TokenInfo
from trading.token_builder import build_pumpfun_token_info
from utils.logger import get_logger

logger = get_logger(__name__)


class DexPaidSource:
    """Polls dex-paid tokens and emits TokenInfos for buying."""

    def __init__(
        self,
        platform_impls: object,
        client: object,
        poll_interval: float = 30.0,
        max_age_minutes: float = 180.0,
    ) -> None:
        self._impls = platform_impls
        self._client = client
        self.poll_interval = poll_interval
        self.max_age_minutes = max_age_minutes
        self._seen: set[str] = set()

    async def run(self, token_callback: Callable[[TokenInfo], Awaitable[None]]) -> None:
        """Continuously poll and emit new, non-migrated dex-paid candidates."""
        from dashboard.dexpaid_feed import get_candidates

        logger.info(
            "DexPaid source active: poll every %ss, max age %s min",
            self.poll_interval,
            self.max_age_minutes,
        )
        while True:
            try:
                candidates = await get_candidates(
                    limit=10, max_age_minutes=self.max_age_minutes, check_locked=False
                )
                for c in candidates:
                    mint = c.get("mint")
                    if not mint or mint in self._seen:
                        continue
                    if c.get("migrated"):
                        continue  # only bonding-curve buys
                    token_info = await build_pumpfun_token_info(
                        mint, self._impls, self._client, symbol=c.get("symbol", "")
                    )
                    if token_info is not None:
                        self._seen.add(mint)
                        logger.info(
                            "DexPaid candidate -> %s (%s), paid %s min ago",
                            token_info.symbol,
                            mint,
                            c.get("payment_age_min"),
                        )
                        await token_callback(token_info)
            except asyncio.CancelledError:
                logger.info("DexPaid source cancelled")
                break
            except Exception:
                logger.exception("DexPaid source error")
            await asyncio.sleep(self.poll_interval)
