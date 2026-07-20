"""Build a TokenInfo for an already-existing mint (not a fresh creation event).

Needed by the buy-on-dex-paid flow: the dex-paid feed gives a mint, and to buy
it through the normal buyer we must reconstruct the same TokenInfo the new-token
listeners build. For pump.fun that means deriving the bonding curve, reading the
curve account for the creator + mayhem/cashback flags, deriving the associated
bonding curve and creator vault, and detecting the actual token program.

IMPORTANT: enforces "not migrated" — if the bonding curve is complete (token
moved to PumpSwap), returns None so we never try a bonding-curve buy on a
migrated token.

WARNING: this path (buying an arbitrary existing mint) has NOT been verified
against a live buy in this workspace. Test with a tiny buy_amount first.
"""

from __future__ import annotations

from solders.pubkey import Pubkey

from interfaces.core import Platform, TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)


async def build_pumpfun_token_info(
    mint_str: str,
    platform_impls: object,
    client: object,
    symbol: str = "",
    name: str = "",
) -> TokenInfo | None:
    """Reconstruct a pump.fun TokenInfo for an existing mint.

    Args:
        mint_str: Token mint address.
        platform_impls: Platform implementations (address_provider,
            curve_manager) for pump.fun.
        client: SolanaClient for the mint-owner lookup.
        symbol: Optional symbol for logs.
        name: Optional name.

    Returns:
        A TokenInfo ready for the buyer, or None if the mint is invalid,
        migrated (curve complete), or the curve can't be read.
    """
    try:
        mint = Pubkey.from_string(mint_str)
    except (ValueError, TypeError):
        logger.warning("Invalid mint for dex-paid buy: %s", mint_str)
        return None

    address_provider = platform_impls.address_provider
    curve_manager = platform_impls.curve_manager
    bonding_curve = address_provider.derive_pool_address(mint)

    # Reject migrated tokens (bonding curve complete -> now on PumpSwap).
    try:
        if await curve_manager.is_curve_complete(bonding_curve):
            logger.info("Skip %s (%s): bonding curve complete (migrated)", symbol, mint_str)
            return None
    except Exception:  # noqa: BLE001 -- unreadable curve => can't safely buy
        logger.warning("Could not read curve for %s; skipping", mint_str)
        return None

    try:
        state = await curve_manager.get_pool_state(bonding_curve)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read curve state for %s; skipping", mint_str)
        return None

    creator_raw = state.get("creator")
    creator = None
    if creator_raw:
        try:
            creator = (
                creator_raw
                if isinstance(creator_raw, Pubkey)
                else Pubkey.from_string(str(creator_raw))
            )
        except (ValueError, TypeError):
            creator = None

    # Determine the actual token program (owner of the mint account). Do NOT
    # guess: a wrong program derives the wrong ATA and the buy fails. If the
    # mint account can't be read, skip this candidate.
    token_program_id = None
    try:
        mint_acct = await client.get_account_info(mint)
        if mint_acct and getattr(mint_acct, "owner", None):
            token_program_id = mint_acct.owner
    except Exception:  # noqa: BLE001
        token_program_id = None
    if token_program_id is None:
        logger.warning(
            "Could not read token program for %s; skipping (won't guess)", mint_str
        )
        return None

    token_info = TokenInfo(
        name=name or symbol or str(mint),
        symbol=symbol or "?",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        creator=creator,
        token_program_id=token_program_id,
        is_mayhem_mode=bool(state.get("is_mayhem_mode", False)),
        is_cashback_coin=bool(state.get("is_cashback_coin", False)),
    )

    # Derive associated bonding curve + creator vault.
    extra = address_provider.get_additional_accounts(token_info)
    token_info.associated_bonding_curve = extra.get("associated_bonding_curve")
    token_info.creator_vault = extra.get("creator_vault")

    if token_info.associated_bonding_curve is None or token_info.creator is None:
        logger.warning(
            "Incomplete accounts for %s (creator=%s); skipping", mint_str, creator
        )
        return None
    return token_info
