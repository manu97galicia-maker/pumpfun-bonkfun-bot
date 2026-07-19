"""Async client for the public DexScreener API.

Covers the endpoints documented at https://docs.dexscreener.com/api/reference:

    - Token profiles      GET /token-profiles/latest/v1                (60 rpm)
    - Token boosts        GET /token-boosts/latest/v1                  (60 rpm)
    - Token boosts (top)  GET /token-boosts/top/v1                     (60 rpm)
    - Orders / paid check GET /orders/v1/{chainId}/{tokenAddress}      (60 rpm)
    - Pair by address     GET /latest/dex/pairs/{chainId}/{pairId}     (300 rpm)
    - Search pairs        GET /latest/dex/search?q=                    (300 rpm)
    - Token pairs         GET /token-pairs/v1/{chainId}/{tokenAddress} (300 rpm)
    - Tokens by address   GET /tokens/v1/{chainId}/{tokenAddresses}    (300 rpm)

On top of the raw endpoints it exposes a couple of convenience helpers:

    - ``is_dex_paid`` / ``get_dex_paid_status`` -- has the token paid DexScreener
      (approved profile / boost / ad order)?
    - ``get_boost_amount`` -- active boost amount for a token.
    - ``get_market_data`` -- best pair's price / liquidity / volume / market cap.

The client owns its own ``aiohttp`` session and is safe to share across
coroutines. It does *not* apply any trading decision; callers decide how to
use the returned data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from core.rpc_rate_limiter import TokenBucketRateLimiter
from utils.logger import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api.dexscreener.com"

# Default chain for this bot. DexScreener uses lowercase chain slugs.
DEFAULT_CHAIN = "solana"

# Order types DexScreener bills for. An approved order of any of these types
# means the token owner has paid DexScreener ("Dex Paid").
PAID_ORDER_TYPES = frozenset(
    {
        "tokenProfile",
        "communityTakeover",
        "tokenAd",
        "trendingBarAd",
    }
)

# DexScreener marks fulfilled orders as "approved".
APPROVED_STATUS = "approved"


@dataclass(slots=True)
class DexPaidStatus:
    """Result of the "Dex Paid" check for a token.

    Attributes:
        paid: True if the token has at least one approved paid order.
        approved_types: Approved (fulfilled) paid order types, e.g.
            ``{"tokenProfile"}``.
        orders: Raw order objects returned by the API, untouched.
    """

    paid: bool
    approved_types: set[str] = field(default_factory=set)
    orders: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TokenMarketData:
    """Condensed market data for a token, taken from its best pair.

    "Best" pair is the one with the highest USD liquidity. All monetary
    fields are USD unless noted. Any field may be ``None`` when DexScreener
    does not report it.

    Attributes:
        pair_address: Address of the pair the data comes from.
        dex_id: DEX the pair trades on (e.g. ``pumpswap``, ``raydium``).
        price_usd: Current token price in USD.
        price_native: Current token price in the quote token.
        liquidity_usd: Pool liquidity in USD.
        volume_h24: 24h volume in USD.
        fdv: Fully diluted valuation in USD.
        market_cap: Market cap in USD.
        pair_created_at: Pair creation time (ms since epoch).
        boosts_active: Active boost count on the pair, if any.
        raw: The full raw pair object.
    """

    pair_address: str | None = None
    dex_id: str | None = None
    price_usd: float | None = None
    price_native: float | None = None
    liquidity_usd: float | None = None
    volume_h24: float | None = None
    fdv: float | None = None
    market_cap: float | None = None
    pair_created_at: int | None = None
    boosts_active: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class DexScreenerClient:
    """Async client for the DexScreener HTTP API.

    Owns an ``aiohttp`` session and two token-bucket rate limiters (one for
    the 60 rpm endpoints, one for the 300 rpm endpoints) so it stays within
    DexScreener's published limits without extra caller bookkeeping.

    Use it as an async context manager, or call :meth:`close` when done::

        async with DexScreenerClient() as dex:
            paid = await dex.is_dex_paid(mint)

    Args:
        chain: Default chain slug used when a call omits one.
        timeout: Per-request timeout in seconds.
        session: Optional externally-managed session to reuse. When provided,
            the client will not close it.
    """

    def __init__(
        self,
        chain: str = DEFAULT_CHAIN,
        timeout: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._chain = chain
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._owns_session = session is None
        self._session_lock = asyncio.Lock()
        # DexScreener limits: 60 rpm for profile/boost/order endpoints,
        # 300 rpm for pair/token/search endpoints. Convert to req/sec.
        self._limiter_60 = TokenBucketRateLimiter(max_rps=1.0, burst_size=5)
        self._limiter_300 = TokenBucketRateLimiter(max_rps=5.0, burst_size=10)

    async def __aenter__(self) -> DexScreenerClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying session if this client created it."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or lazily create the shared aiohttp session."""
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(timeout=self._timeout)
                    self._owns_session = True
        return self._session

    async def _get(
        self,
        path: str,
        limiter: TokenBucketRateLimiter,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a rate-limited GET and return the decoded JSON body.

        Args:
            path: Path relative to the API base URL (leading slash included).
            limiter: Rate limiter to acquire before the request.
            params: Optional query parameters.

        Returns:
            Decoded JSON (dict or list). Returns ``None`` on request/decode
            errors so callers can degrade gracefully instead of crashing the
            trading loop.
        """
        await limiter.acquire()
        session = await self._get_session()
        url = f"{BASE_URL}{path}"
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    logger.warning("DexScreener rate limited (429) on %s", path)
                    return None
                if resp.status >= 400:
                    logger.warning(
                        "DexScreener %s returned HTTP %s", path, resp.status
                    )
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("DexScreener request to %s failed: %s", path, exc)
            return None

    # ------------------------------------------------------------------ #
    # Raw endpoints                                                       #
    # ------------------------------------------------------------------ #

    async def get_latest_token_profiles(self) -> list[dict[str, Any]]:
        """GET /token-profiles/latest/v1 -- latest token profiles."""
        data = await self._get("/token-profiles/latest/v1", self._limiter_60)
        return _as_list(data)

    async def get_latest_boosts(self) -> list[dict[str, Any]]:
        """GET /token-boosts/latest/v1 -- latest boosted tokens."""
        data = await self._get("/token-boosts/latest/v1", self._limiter_60)
        return _as_list(data)

    async def get_top_boosts(self) -> list[dict[str, Any]]:
        """GET /token-boosts/top/v1 -- tokens with most active boosts."""
        data = await self._get("/token-boosts/top/v1", self._limiter_60)
        return _as_list(data)

    async def get_orders(
        self, token_address: str, chain: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /orders/v1/{chain}/{token} -- paid orders for a token.

        Each order looks like ``{"type": "tokenProfile", "status":
        "approved", "paymentTimestamp": ...}``.

        Args:
            token_address: Token mint address.
            chain: Chain slug; defaults to the client's chain.

        Returns:
            List of order objects (possibly empty).
        """
        chain = chain or self._chain
        data = await self._get(
            f"/orders/v1/{chain}/{token_address}", self._limiter_60
        )
        return _flatten_orders(data)

    async def get_pair(
        self, pair_address: str, chain: str | None = None
    ) -> dict[str, Any] | None:
        """GET /latest/dex/pairs/{chain}/{pair} -- a single pair by address."""
        chain = chain or self._chain
        data = await self._get(
            f"/latest/dex/pairs/{chain}/{pair_address}", self._limiter_300
        )
        pairs = _extract_pairs(data)
        return pairs[0] if pairs else None

    async def search_pairs(self, query: str) -> list[dict[str, Any]]:
        """GET /latest/dex/search?q= -- search pairs by name/symbol/address."""
        data = await self._get(
            "/latest/dex/search", self._limiter_300, params={"q": query}
        )
        return _extract_pairs(data)

    async def get_token_pairs(
        self, token_address: str, chain: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /token-pairs/v1/{chain}/{token} -- all pairs for a token."""
        chain = chain or self._chain
        data = await self._get(
            f"/token-pairs/v1/{chain}/{token_address}", self._limiter_300
        )
        return _as_list(data)

    async def get_tokens(
        self, token_addresses: str | list[str], chain: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /tokens/v1/{chain}/{addresses} -- pairs for up to 30 tokens.

        Args:
            token_addresses: A single address or a list (max 30). A list is
                joined with commas as the API expects.
            chain: Chain slug; defaults to the client's chain.
        """
        chain = chain or self._chain
        if isinstance(token_addresses, list):
            token_addresses = ",".join(token_addresses)
        data = await self._get(
            f"/tokens/v1/{chain}/{token_addresses}", self._limiter_300
        )
        return _as_list(data)

    # ------------------------------------------------------------------ #
    # Convenience helpers                                                 #
    # ------------------------------------------------------------------ #

    async def get_dex_paid_status(
        self, token_address: str, chain: str | None = None
    ) -> DexPaidStatus:
        """Check whether a token has paid DexScreener ("Dex Paid").

        A token is considered paid when it has at least one approved order of
        a billed type (profile / community takeover / ad).

        Args:
            token_address: Token mint address.
            chain: Chain slug; defaults to the client's chain.

        Returns:
            A :class:`DexPaidStatus` with the verdict, the approved order
            types, and the raw orders.
        """
        orders = await self.get_orders(token_address, chain)
        approved = {
            order.get("type")
            for order in orders
            if order.get("status") == APPROVED_STATUS
            and order.get("type") in PAID_ORDER_TYPES
        }
        approved.discard(None)
        return DexPaidStatus(
            paid=bool(approved),
            approved_types=approved,  # type: ignore[arg-type]
            orders=orders,
        )

    async def is_dex_paid(
        self, token_address: str, chain: str | None = None
    ) -> bool:
        """Return True if the token has an approved paid DexScreener order."""
        status = await self.get_dex_paid_status(token_address, chain)
        return status.paid

    async def get_boost_amount(
        self, token_address: str, chain: str | None = None
    ) -> float:
        """Return the active boost amount for a token (0.0 if none/unknown)."""
        pairs = await self.get_token_pairs(token_address, chain)
        for pair in pairs:
            boosts = pair.get("boosts") or {}
            active = boosts.get("active")
            if active:
                return float(active)
        return 0.0

    async def get_market_data(
        self, token_address: str, chain: str | None = None
    ) -> TokenMarketData | None:
        """Return condensed market data from the token's most-liquid pair.

        Args:
            token_address: Token mint address.
            chain: Chain slug; defaults to the client's chain.

        Returns:
            A :class:`TokenMarketData`, or ``None`` if DexScreener has no
            pair for the token yet (common right after launch).
        """
        pairs = await self.get_token_pairs(token_address, chain)
        if not pairs:
            return None
        best = max(pairs, key=lambda p: _num((p.get("liquidity") or {}).get("usd")) or 0)
        liquidity = best.get("liquidity") or {}
        volume = best.get("volume") or {}
        price_native = best.get("priceNative")
        boosts = best.get("boosts") or {}
        return TokenMarketData(
            pair_address=best.get("pairAddress"),
            dex_id=best.get("dexId"),
            price_usd=_num(best.get("priceUsd")),
            price_native=_num(price_native),
            liquidity_usd=_num(liquidity.get("usd")),
            volume_h24=_num(volume.get("h24")),
            fdv=_num(best.get("fdv")),
            market_cap=_num(best.get("marketCap")),
            pair_created_at=best.get("pairCreatedAt"),
            boosts_active=boosts.get("active"),
            raw=best,
        )


def _as_list(data: Any) -> list[dict[str, Any]]:
    """Coerce an API response into a list of dicts."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _flatten_orders(data: Any) -> list[dict[str, Any]]:
    """Normalize the /orders/v1 response into a flat list of order dicts.

    DexScreener's documented shape is a flat array of
    ``{"type", "status", "paymentTimestamp"}`` objects, but the live API
    can wrap them as ``[{"orders": [...], "boosts": [...]}]``. Handle both:
    an item that carries a nested ``orders`` list is expanded; an item that
    already looks like an order (has a ``type``) is kept as-is.
    """
    orders: list[dict[str, Any]] = []
    for item in _as_list(data):
        nested = item.get("orders")
        if isinstance(nested, list):
            orders.extend(o for o in nested if isinstance(o, dict))
        elif "type" in item:
            orders.append(item)
    return orders


def _extract_pairs(data: Any) -> list[dict[str, Any]]:
    """Pull the ``pairs`` array out of a /latest/dex response."""
    if isinstance(data, dict):
        pairs = data.get("pairs")
        if isinstance(pairs, list):
            return [p for p in pairs if isinstance(p, dict)]
    return []


def _num(value: Any) -> float | None:
    """Best-effort float conversion; None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
