"""Config-driven DexScreener buy filter.

Sits between token detection and the buy in :class:`UniversalTrader`. It is
fully driven by the ``filters.dexscreener`` block of a bot config and is
**disabled by default** -- with no config, or ``enabled: false``, it lets every
token through, so existing bots are unaffected.

When enabled it can gate buys on:
    - Dex Paid       -- token has an approved paid DexScreener order.
    - Active boosts  -- minimum boost count.
    - Liquidity / 24h volume / market-cap window.
    - Listed         -- token already indexed on DexScreener.

``enforce: true`` blocks the buy on failure; ``enforce: false`` only logs
(dry-run), so you can watch what the filter *would* do before trusting it with
real orders.

Note: enabling this adds an HTTP round-trip to DexScreener before each buy,
which costs latency. Freshly-minted pump.fun tokens are usually not indexed on
DexScreener for the first seconds, so a strict filter (e.g. ``require_listed``
or a ``min_liquidity_usd``) will skip most brand-new snipes by design.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from dexscreener.client import (
    DEFAULT_CHAIN,
    DexPaidStatus,
    DexScreenerClient,
    TokenMarketData,
)
from dexscreener.rugcheck import RugCheckClient
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class FilterDecision:
    """Outcome of evaluating a token against the DexScreener filter.

    Attributes:
        allowed: True if the token passes every configured criterion.
        reasons: Human-readable reasons the token was blocked (empty if
            allowed).
        market: Market data fetched during the check, if any.
        dex_paid: Dex Paid status fetched during the check, if any.
    """

    allowed: bool
    reasons: list[str] = field(default_factory=list)
    market: TokenMarketData | None = None
    dex_paid: DexPaidStatus | None = None


class DexScreenerFilter:
    """Evaluate tokens against DexScreener criteria before buying.

    Args:
        config: The ``filters.dexscreener`` config dict (may be ``None`` or
            empty -- the filter is then disabled).
        client: Optional shared :class:`DexScreenerClient`. When omitted the
            filter creates and owns one.
    """

    def __init__(
        self,
        config: dict | None,
        client: DexScreenerClient | None = None,
    ) -> None:
        config = config or {}
        self.enabled: bool = bool(config.get("enabled", False))
        # enforce=True blocks the buy on failure; False = log-only (dry run).
        self.enforce: bool = bool(config.get("enforce", True))
        # When a check can't complete (timeout/API error): True = block the buy
        # (fail-closed, safest for real money), False = let it through.
        self.block_on_error: bool = bool(config.get("block_on_error", True))
        self.chain: str = config.get("chain") or DEFAULT_CHAIN

        # Criteria (all optional; 0/False means "don't check this").
        self.require_listed: bool = bool(config.get("require_listed", False))
        self.require_dex_paid: bool = bool(config.get("require_dex_paid", False))
        self.min_boosts: float = float(config.get("min_boosts", 0) or 0)
        self.min_liquidity_usd: float = float(config.get("min_liquidity_usd", 0) or 0)
        self.min_volume_h24: float = float(config.get("min_volume_h24", 0) or 0)
        self.min_market_cap: float = float(config.get("min_market_cap", 0) or 0)
        self.max_market_cap: float = float(config.get("max_market_cap", 0) or 0)
        self.timeout_seconds: float = float(config.get("timeout_seconds", 5) or 5)

        # OR group: at least one of these must pass (e.g. dex paid OR boosts>=100).
        # Supported keys: require_dex_paid (bool), min_boosts, min_liquidity_usd,
        # min_volume_h24, min_market_cap.
        self.any_of: dict = config.get("any_of") or {}

        # Locked-liquidity check via RugCheck (DexScreener can't provide it).
        self.require_liquidity_locked: bool = bool(
            config.get("require_liquidity_locked", False)
        )
        self.min_lp_locked_pct: float = float(config.get("min_lp_locked_pct", 50) or 50)
        self.block_if_rugged: bool = bool(config.get("block_if_rugged", True))

        self._owns_client = client is None
        self._client = client or DexScreenerClient(
            chain=self.chain, timeout=self.timeout_seconds
        )
        self._rug: RugCheckClient | None = None

    def _rugcheck(self) -> RugCheckClient:
        if self._rug is None:
            self._rug = RugCheckClient(timeout=self.timeout_seconds)
        return self._rug

    def _any_of_needs_market(self) -> bool:
        return any(
            k in self.any_of and self.any_of.get(k)
            for k in ("min_boosts", "min_liquidity_usd", "min_volume_h24", "min_market_cap")
        )

    @property
    def _needs_market_data(self) -> bool:
        """Whether any configured criterion requires pair/market data."""
        return (
            self.require_listed
            or self.min_boosts > 0
            or self.min_liquidity_usd > 0
            or self.min_volume_h24 > 0
            or self.min_market_cap > 0
            or self.max_market_cap > 0
            or self._any_of_needs_market()
        )

    def describe(self) -> str:
        """One-line summary of the active criteria, for startup logging."""
        if not self.enabled:
            return "DexScreener filter: disabled"
        parts: list[str] = []
        if self.require_dex_paid:
            parts.append("dex_paid")
        if self.require_listed:
            parts.append("listed")
        if self.min_boosts > 0:
            parts.append(f"boosts>={self.min_boosts:g}")
        if self.min_liquidity_usd > 0:
            parts.append(f"liq>=${self.min_liquidity_usd:g}")
        if self.min_volume_h24 > 0:
            parts.append(f"vol24>=${self.min_volume_h24:g}")
        if self.min_market_cap > 0:
            parts.append(f"mcap>=${self.min_market_cap:g}")
        if self.max_market_cap > 0:
            parts.append(f"mcap<=${self.max_market_cap:g}")
        criteria = ", ".join(parts) if parts else "no criteria set"
        mode = "enforce" if self.enforce else "log-only"
        return f"DexScreener filter: enabled ({mode}) -- {criteria}"

    async def evaluate(self, mint: str) -> FilterDecision:
        """Evaluate a single token mint against the configured criteria.

        Network work is bounded by ``timeout_seconds``. On timeout or API
        error the decision follows ``block_on_error``.

        Args:
            mint: Token mint address as a string.

        Returns:
            A :class:`FilterDecision`.
        """
        try:
            return await asyncio.wait_for(
                self._evaluate(mint), timeout=self.timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 -- fail per block_on_error policy
            logger.warning(
                "DexScreener filter check failed for %s: %s", mint, exc
            )
            if self.block_on_error:
                return FilterDecision(
                    allowed=False, reasons=[f"check error: {exc}"]
                )
            return FilterDecision(allowed=True, reasons=[])

    async def _evaluate(self, mint: str) -> FilterDecision:
        reasons: list[str] = []
        dex_paid: DexPaidStatus | None = None
        market: TokenMarketData | None = None

        need_paid = self.require_dex_paid or bool(self.any_of.get("require_dex_paid"))
        if need_paid:
            dex_paid = await self._client.get_dex_paid_status(mint, self.chain)
            if self.require_dex_paid and not dex_paid.paid:
                reasons.append("not Dex Paid")

        if self._needs_market_data:
            market = await self._client.get_market_data(mint, self.chain)
            if market is None:
                # No pair indexed yet. Anything that needs market data fails.
                reasons.append("no DexScreener pair (not listed)")
            else:
                boosts = float(market.boosts_active or 0)
                if self.min_boosts > 0 and boosts < self.min_boosts:
                    reasons.append(f"boosts {boosts:g} < {self.min_boosts:g}")

                liq = market.liquidity_usd or 0.0
                if self.min_liquidity_usd > 0 and liq < self.min_liquidity_usd:
                    reasons.append(
                        f"liquidity ${liq:,.0f} < ${self.min_liquidity_usd:,.0f}"
                    )

                vol = market.volume_h24 or 0.0
                if self.min_volume_h24 > 0 and vol < self.min_volume_h24:
                    reasons.append(
                        f"volume24h ${vol:,.0f} < ${self.min_volume_h24:,.0f}"
                    )

                mcap = market.market_cap or 0.0
                if self.min_market_cap > 0 and mcap < self.min_market_cap:
                    reasons.append(
                        f"marketcap ${mcap:,.0f} < ${self.min_market_cap:,.0f}"
                    )
                if self.max_market_cap > 0 and mcap > self.max_market_cap:
                    reasons.append(
                        f"marketcap ${mcap:,.0f} > ${self.max_market_cap:,.0f}"
                    )

        # OR group: at least one condition must pass.
        if self.any_of:
            passed, fails = self._eval_any_of(dex_paid, market)
            if not passed:
                reasons.append("ninguna condición OR cumplida (" + ", ".join(fails) + ")")

        # Locked liquidity via RugCheck.
        if self.require_liquidity_locked:
            report = await self._rugcheck().get_report(mint)
            if not report.found:
                reasons.append("RugCheck sin datos de liquidez")
            else:
                if self.block_if_rugged and report.rugged:
                    reasons.append("RugCheck: marcado como rugged")
                if report.lp_locked_pct < self.min_lp_locked_pct:
                    reasons.append(
                        f"liquidez bloqueada {report.lp_locked_pct:.0f}% "
                        f"< {self.min_lp_locked_pct:.0f}%"
                    )

        return FilterDecision(
            allowed=not reasons,
            reasons=reasons,
            market=market,
            dex_paid=dex_paid,
        )

    def _eval_any_of(
        self, dex_paid: DexPaidStatus | None, market: TokenMarketData | None
    ) -> tuple[bool, list[str]]:
        """Evaluate the OR group. Returns (at_least_one_passed, fail_reasons)."""
        passed: list[str] = []
        fails: list[str] = []
        a = self.any_of

        if a.get("require_dex_paid"):
            if dex_paid and dex_paid.paid:
                passed.append("dex_paid")
            else:
                fails.append("no dex paid")

        def _market_cond(key: str, attr: str, label: str) -> None:
            threshold = float(a.get(key) or 0)
            if threshold <= 0:
                return
            value = float(getattr(market, attr, 0) or 0) if market else 0.0
            if value >= threshold:
                passed.append(f"{label} {value:g}")
            else:
                fails.append(f"{label} {value:g}<{threshold:g}")

        _market_cond("min_boosts", "boosts_active", "boosts")
        _market_cond("min_liquidity_usd", "liquidity_usd", "liq")
        _market_cond("min_volume_h24", "volume_h24", "vol24")
        _market_cond("min_market_cap", "market_cap", "mcap")

        return (len(passed) > 0, fails)

    async def should_buy(self, mint: str, symbol: str = "") -> bool:
        """Decide whether the trader should proceed with the buy.

        Applies ``enabled`` and ``enforce``: a disabled filter always returns
        True; in log-only mode it logs the verdict but still returns True.

        Args:
            mint: Token mint address as a string.
            symbol: Token symbol, for readable logs.

        Returns:
            True if the buy should proceed, False if it should be skipped.
        """
        if not self.enabled:
            return True

        decision = await self.evaluate(mint)
        label = f"{symbol} ({mint})" if symbol else mint

        if decision.allowed:
            logger.info("DexScreener filter PASS for %s", label)
            return True

        reason_text = "; ".join(decision.reasons) or "criteria not met"
        if self.enforce:
            logger.info("DexScreener filter BLOCK for %s -- %s", label, reason_text)
            return False

        logger.info(
            "DexScreener filter would block %s (log-only) -- %s", label, reason_text
        )
        return True

    async def close(self) -> None:
        """Close the owned DexScreener + RugCheck clients, if any."""
        if self._owns_client:
            await self._client.close()
        if self._rug is not None:
            await self._rug.close()
