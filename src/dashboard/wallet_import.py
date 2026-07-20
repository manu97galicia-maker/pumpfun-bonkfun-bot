"""Derive a Solana wallet from a BIP39 mnemonic (12 or 24 words).

Uses the standard Phantom/Solflare derivation path ``m/44'/501'/{account}'/0'``
so the imported address matches what those wallets show for the same seed.

Security: the mnemonic is used only to derive the key and is never stored or
logged here. The caller decides where to persist the derived private key.
"""

from __future__ import annotations

import base58
from solders.keypair import Keypair

DERIVATION_PATH = "m/44'/501'/{account}'/0'"


def derive_from_mnemonic(mnemonic: str, account: int = 0) -> dict[str, str]:
    """Derive a Solana keypair from a BIP39 mnemonic.

    Args:
        mnemonic: 12- or 24-word BIP39 seed phrase.
        account: Account index (0 = the first Phantom account).

    Returns:
        ``{"pubkey": <address>, "private_key_b58": <base58 64-byte key>}``.
        The base58 private key is the format the bot expects in
        ``SOLANA_PRIVATE_KEY``.

    Raises:
        ValueError: If the word count is wrong or the mnemonic is invalid.
    """
    from bip_utils import (
        Bip44,
        Bip44Changes,
        Bip44Coins,
        Bip39SeedGenerator,
    )

    words = mnemonic.strip().split()
    if len(words) not in (12, 24):
        msg = f"Expected 12 or 24 words, got {len(words)}"
        raise ValueError(msg)
    normalized = " ".join(words).lower()

    try:
        # Bip39SeedGenerator validates the checksum and raises on a bad phrase.
        seed_bytes = Bip39SeedGenerator(normalized).Generate()
    except Exception as exc:  # noqa: BLE001 -- normalize any bip_utils error
        msg = "Invalid seed phrase (check the words and their order)"
        raise ValueError(msg) from exc

    if account < 0:
        raise ValueError("account index must be >= 0")

    node = (
        Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA)
        .Purpose()
        .Coin()
        .Account(account)
        .Change(Bip44Changes.CHAIN_EXT)
    )
    priv32 = node.PrivateKey().Raw().ToBytes()
    keypair = Keypair.from_seed(priv32)
    return {
        "pubkey": str(keypair.pubkey()),
        "private_key_b58": base58.b58encode(bytes(keypair)).decode(),
    }
