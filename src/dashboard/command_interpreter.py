"""Turn plain-language orders into concrete config changes.

Rule-based (no LLM needed): the dashboard command box sends text like
"compra cuando dex paid" or "tp 40% vende 80%, sl 18%" and this maps it to a
list of actions the config editor can apply. Anything not understood is
returned in ``unmatched`` so the UI can say so instead of silently ignoring.

Each action is ``{scope, field, value, human}`` where ``scope`` is
``"trade"`` (edits ``trade.*``) or ``"dexscreener"`` (edits
``filters.dexscreener.*``).
"""

from __future__ import annotations

import re
from typing import Any


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def interpret(text: str) -> dict[str, Any]:
    """Parse an order string into config actions.

    Args:
        text: Free-form instruction (Spanish or English).

    Returns:
        ``{"actions": [...], "matched": bool, "note": str}``.
    """
    t = " " + text.lower().strip() + " "
    actions: list[dict[str, Any]] = []

    def add(scope: str, field: str, value: Any, human: str) -> None:
        actions.append({"scope": scope, "field": field, "value": value, "human": human})

    # --- DexScreener "Dex Paid" gate ---
    if re.search(r"dex\s*paid", t):
        if re.search(r"\b(no|sin|nunca|solo si no)\b.*dex\s*paid", t):
            add("dexscreener", "enabled", False, "Desactivar filtro Dex Paid")
        else:
            add("dexscreener", "enabled", True, "Activar filtro DexScreener")
            add("dexscreener", "require_dex_paid", True, "Comprar solo si Dex Paid")
            add("dexscreener", "enforce", True, "Bloquear compra si no cumple")

    if re.search(r"(desactiv\w*|quita\w*|apaga\w*)\s+(el\s+)?filtro", t):
        add("dexscreener", "enabled", False, "Desactivar el filtro DexScreener")

    # --- DexScreener numeric thresholds ---
    m = re.search(r"liquidez\s*(?:min\w*\s*)?\$?\s*(\d[\d.]*)", t)
    if m:
        add("dexscreener", "enabled", True, "Activar filtro DexScreener")
        add("dexscreener", "min_liquidity_usd", _num(m.group(1)), f"Liquidez mínima ${m.group(1)}")
    m = re.search(r"boost\w*\s*(?:min\w*\s*)?(\d+)", t)
    if m:
        add("dexscreener", "enabled", True, "Activar filtro DexScreener")
        add("dexscreener", "min_boosts", int(m.group(1)), f"Boosts mínimos {m.group(1)}")
    m = re.search(r"market\s*cap\s*(?:min\w*\s*)?\$?\s*(\d[\d.]*)", t)
    if m:
        add("dexscreener", "enabled", True, "Activar filtro DexScreener")
        add("dexscreener", "min_market_cap", _num(m.group(1)), f"Market cap mínimo ${m.group(1)}")

    # --- Take profit (tier 1) with optional partial sell ---
    m = re.search(
        r"(?:tp|take\s*profit|vend\w*)\s*(\d+)\s*%[^%]*?(?:vend\w*\s*(\d+)\s*%|(\d+)\s*%)?",
        t,
    )
    if m and ("tp" in t or "take profit" in t or "vend" in t):
        gain = int(m.group(1))
        sell = m.group(2) or m.group(3)
        add("trade", "exit_strategy", "tp_sl", "Estrategia TP/SL")
        add("trade", "take_profit_percentage", gain / 100, f"Take profit +{gain}%")
        if sell:
            add("trade", "take_profit_sell_percentage", int(sell) / 100, f"Vender {sell}% en TP")

    # --- Stop loss ---
    m = re.search(r"(?:sl|stop\s*loss)\s*(\d+)\s*%", t)
    if m:
        add("trade", "exit_strategy", "tp_sl", "Estrategia TP/SL")
        add("trade", "stop_loss_percentage", int(m.group(1)) / 100, f"Stop loss -{m.group(1)}%")

    # --- Slippage ---
    m = re.search(r"slippage\s*(\d+)\s*%", t)
    if m:
        val = int(m.group(1)) / 100
        add("trade", "buy_slippage", val, f"Slippage compra {m.group(1)}%")
        add("trade", "sell_slippage", val, f"Slippage venta {m.group(1)}%")

    # --- Buy amount ---
    m = re.search(r"compr\w*\s*(?:con\s*)?(\d+(?:[.,]\d+)?)\s*sol", t)
    if m:
        add("trade", "buy_amount", _num(m.group(1)), f"Comprar con {m.group(1)} SOL por token")

    # De-duplicate (keep last value per scope+field)
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for a in actions:
        dedup[(a["scope"], a["field"])] = a
    final = list(dedup.values())

    return {
        "actions": final,
        "matched": bool(final),
        "note": "" if final else "No entendí ninguna orden. Ejemplos: 'compra cuando dex paid', "
        "'tp 40% vende 80%', 'sl 18%', 'liquidez min 5000', 'slippage 30%'.",
    }
