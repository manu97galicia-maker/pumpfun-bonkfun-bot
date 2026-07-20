"""Minimal async RugCheck client for liquidity-lock / rug checks.

Used by the DexScreener buy filter to enforce "locked liquidity" (which the
DexScreener API does not expose). Reads the public RugCheck report
(https://api.rugcheck.xyz) — no auth needed for the basic report.

Only the fields the filter needs are surfaced: the max locked-LP percentage
across markets, the rugged flag and the normalised score.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from core.rpc_rate_limiter import TokenBucketRateLimiter
from utils.logger import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api.rugcheck.xyz/v1"


@dataclass(slots=True)
class RugReport:
    """Condensed RugCheck report.

    Attributes:
        found: True if RugCheck returned a report for the mint.
        rugged: RugCheck's rugged flag.
        score: Normalised risk score (lower is safer on RugCheck's scale).
        lp_locked_pct: Highest locked-LP percentage across markets (0-100).
        total_liquidity_usd: Total market liquidity in USD.
        risks: List of (name, level) risk tuples.
    """

    found: bool = False
    rugged: bool | None = None
    score: float | None = None
    lp_locked_pct: float = 0.0
    total_liquidity_usd: float | None = None
    risks: list[tuple[str, str]] = field(default_factory=list)


class RugCheckClient:
    """Async client for the public RugCheck token report."""

    def __init__(
        self,
        timeout: float = 8.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._owns_session = session is None
        self._lock = asyncio.Lock()
        # RugCheck is unauthenticated and rate-limited; keep it gentle.
        self._limiter = TokenBucketRateLimiter(max_rps=2.0, burst_size=4)

    async def __aenter__(self) -> RugCheckClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(timeout=self._timeout)
                    self._owns_session = True
        return self._session

    async def get_report(self, mint: str) -> RugReport:
        """Fetch and condense the RugCheck report for a mint.

        Returns a report with ``found=False`` on any error (so the filter can
        decide via its fail-open/closed policy rather than crashing).
        """
        await self._limiter.acquire()
        session = await self._get_session()
        url = f"{BASE_URL}/tokens/{mint}/report"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("RugCheck %s -> HTTP %s", mint, resp.status)
                    return RugReport(found=False)
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("RugCheck request failed for %s: %s", mint, exc)
            return RugReport(found=False)
        return _parse_report(data)

    async def is_liquidity_locked(self, mint: str, min_pct: float = 50.0) -> bool:
        """True if the max locked-LP percentage meets ``min_pct``."""
        report = await self.get_report(mint)
        return report.found and report.lp_locked_pct >= min_pct


def _parse_report(data: Any) -> RugReport:
    if not isinstance(data, dict):
        return RugReport(found=False)
    markets = data.get("markets") or []
    lp_locked_pct = 0.0
    for market in markets:
        lp = (market or {}).get("lp") or {}
        pct = _num(lp.get("lpLockedPct"))
        if pct is not None:
            lp_locked_pct = max(lp_locked_pct, pct)
    risks = [
        (r.get("name", "?"), r.get("level", "?"))
        for r in (data.get("risks") or [])
        if isinstance(r, dict)
    ]
    return RugReport(
        found=True,
        rugged=data.get("rugged"),
        score=_num(data.get("score_normalised")),
        lp_locked_pct=lp_locked_pct,
        total_liquidity_usd=_num(data.get("totalMarketLiquidity")),
        risks=risks,
    )


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
