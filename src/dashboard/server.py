"""Local web server for the trading dashboard.

Serves a single-page "robot" UI plus a small JSON API over the bot's live
state: coins being bought, open/closed positions, a highlighted results block,
a strategy-analysis endpoint, and a wallet panel (balance + deposit address).

Design choices for safety:
    - Binds to 127.0.0.1 only (never exposed to the network).
    - Read-only by default. It reads ``trades/trades.log`` and queries the RPC
      for the wallet balance; it never touches the bot's execution.
    - Withdrawing real SOL is OFF unless ``DASHBOARD_ALLOW_WITHDRAW=true`` is
      set in the environment. Even then it only accepts requests from
      localhost. Deposit (showing the address) is always safe.

Run it with::

    python -m dashboard                 # from src/ on PYTHONPATH
    uv run src/dashboard/server.py      # or directly

Environment (loaded from the same .env the bot uses):
    SOLANA_NODE_RPC_ENDPOINT  -- RPC used for balance / withdraw.
    SOLANA_PRIVATE_KEY        -- base58 key; only the pubkey is shown unless
                                 a withdraw is explicitly allowed + requested.
    DASHBOARD_ALLOW_WITHDRAW  -- "true" to enable the withdraw endpoint.
    DASHBOARD_PORT            -- port (default 8787).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from dashboard.analytics import (
    build_positions,
    live_buys,
    load_trades,
    strategy_analysis,
    summarize,
)

STATIC_DIR = Path(__file__).parent / "static"
LAMPORTS_PER_SOL = 1_000_000_000


# --------------------------------------------------------------------------- #
# Wallet helpers (kept dependency-light; no platform imports)                 #
# --------------------------------------------------------------------------- #

def _load_keypair() -> Any | None:
    """Load the wallet keypair from SOLANA_PRIVATE_KEY, or None if unset."""
    key = os.getenv("SOLANA_PRIVATE_KEY")
    if not key:
        return None
    from solders.keypair import Keypair

    key = key.strip()
    try:
        return Keypair.from_base58_string(key)
    except Exception:  # noqa: BLE001 -- try the JSON-array format next
        try:
            return Keypair.from_bytes(bytes(json.loads(key)))
        except Exception:  # noqa: BLE001
            return None


async def _get_balance_sol(pubkey: Any) -> float | None:
    """Query the wallet SOL balance via RPC. None if no RPC configured."""
    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    if not rpc:
        return None
    from solana.rpc.async_api import AsyncClient

    async with AsyncClient(rpc) as client:
        resp = await client.get_balance(pubkey)
        return resp.value / LAMPORTS_PER_SOL


def _build_alerts(positions: dict[str, list[dict]], summary: dict) -> list[dict]:
    """Derive simple alerts from the current state.

    Rugs (big losses), current net result and open exposure. Purely derived;
    no external alert source.
    """
    alerts: list[dict] = []
    for p in positions["closed"][:20]:
        if p["realized_pnl_pct"] <= -50:
            alerts.append(
                {
                    "level": "danger",
                    "text": f"Rug/loss on {p['symbol']}: "
                    f"{p['realized_pnl_pct']:.0f}% ({p['realized_pnl_sol']:.6f} SOL)",
                }
            )
        elif p["realized_pnl_pct"] >= 50:
            alerts.append(
                {
                    "level": "good",
                    "text": f"Winner {p['symbol']}: +{p['realized_pnl_pct']:.0f}% "
                    f"({p['realized_pnl_sol']:.6f} SOL)",
                }
            )
    if summary["open_count"]:
        alerts.append(
            {
                "level": "info",
                "text": f"{summary['open_count']} open position(s), "
                f"{summary['open_invested_sol']:.6f} SOL exposed",
            }
        )
    if summary["total_realized_sol"] < 0:
        alerts.append(
            {
                "level": "danger",
                "text": f"Net result negative: {summary['total_realized_sol']:.6f} SOL",
            }
        )
    return alerts[:12]


# --------------------------------------------------------------------------- #
# HTTP handlers                                                               #
# --------------------------------------------------------------------------- #

def _trades_path(app: web.Application) -> Path:
    return app["trades_log"]


async def handle_index(request: web.Request) -> web.Response:
    """Serve the dashboard page."""
    index = STATIC_DIR / "index.html"
    return web.Response(text=index.read_text(encoding="utf-8"), content_type="text/html")


async def handle_state(request: web.Request) -> web.Response:
    """Return the full dashboard state: results, positions, live buys, alerts."""
    trades = load_trades(_trades_path(request.app))
    positions = build_positions(trades)
    summary = summarize(positions)
    state = {
        "results": summary,
        "open_positions": positions["open"],
        "closed_positions": positions["closed"][:50],
        "live_buys": live_buys(trades, limit=30),
        "alerts": _build_alerts(positions, summary),
        "wallet": await _wallet_info(),
    }
    return web.json_response(state)


async def _wallet_info() -> dict[str, Any]:
    """Wallet panel data: deposit address + live balance."""
    keypair = _load_keypair()
    if keypair is None:
        return {"configured": False, "address": None, "balance_sol": None}
    pubkey = keypair.pubkey()
    balance = await _get_balance_sol(pubkey)
    return {
        "configured": True,
        "address": str(pubkey),
        "balance_sol": balance,
        "withdraw_enabled": os.getenv("DASHBOARD_ALLOW_WITHDRAW", "").lower() == "true",
    }


async def handle_wallet(request: web.Request) -> web.Response:
    """Return only the wallet panel data."""
    return web.json_response(await _wallet_info())


async def handle_analysis(request: web.Request) -> web.Response:
    """Run strategy analysis with user-provided params (JSON body)."""
    try:
        params = await request.json()
    except json.JSONDecodeError:
        params = {}
    trades = load_trades(_trades_path(request.app))
    return web.json_response(strategy_analysis(trades, params))


async def handle_withdraw(request: web.Request) -> web.Response:
    """Withdraw real SOL. Disabled unless explicitly allowed; localhost only.

    Body: ``{"destination": "<pubkey>", "amount_sol": <float>}``.
    """
    if os.getenv("DASHBOARD_ALLOW_WITHDRAW", "").lower() != "true":
        return web.json_response(
            {"ok": False, "error": "Withdraw disabled. Set DASHBOARD_ALLOW_WITHDRAW=true."},
            status=403,
        )
    # Defence in depth: only accept from localhost even though we bind local.
    peer = request.transport.get_extra_info("peername")
    if not peer or peer[0] not in ("127.0.0.1", "::1"):
        return web.json_response({"ok": False, "error": "Local requests only"}, status=403)

    try:
        body = await request.json()
        destination = str(body["destination"])
        amount_sol = float(body["amount_sol"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "Expected {destination, amount_sol}"}, status=400
        )
    if amount_sol <= 0:
        return web.json_response({"ok": False, "error": "amount_sol must be > 0"}, status=400)

    try:
        sig = await _send_sol(destination, amount_sol)
    except Exception as exc:  # noqa: BLE001 -- surface the failure to the UI
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
    return web.json_response({"ok": True, "signature": sig, "amount_sol": amount_sol})


async def _send_sol(destination: str, amount_sol: float) -> str:
    """Build, sign and send a native SOL transfer. Returns the tx signature."""
    keypair = _load_keypair()
    if keypair is None:
        raise ValueError("Wallet not configured (SOLANA_PRIVATE_KEY missing)")
    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    if not rpc:
        raise ValueError("RPC not configured (SOLANA_NODE_RPC_ENDPOINT missing)")

    from solana.rpc.async_api import AsyncClient
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction

    dest = Pubkey.from_string(destination)
    lamports = int(round(amount_sol * LAMPORTS_PER_SOL))
    ix = transfer(
        TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=dest, lamports=lamports)
    )
    async with AsyncClient(rpc) as client:
        blockhash = (await client.get_latest_blockhash()).value.blockhash
        msg = Message.new_with_blockhash([ix], keypair.pubkey(), blockhash)
        tx = Transaction([keypair], msg, blockhash)
        resp = await client.send_transaction(tx)
        return str(resp.value)


def create_app(trades_log: str | Path | None = None) -> web.Application:
    """Build the aiohttp application."""
    app = web.Application()
    app["trades_log"] = Path(trades_log) if trades_log else Path("trades") / "trades.log"
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/wallet", handle_wallet)
    app.router.add_post("/api/analysis", handle_analysis)
    app.router.add_post("/api/withdraw", handle_withdraw)
    return app


def main() -> None:
    """Entry point: load .env and run the dashboard on localhost."""
    try:
        from dotenv import load_dotenv

        # Load .env from cwd if present (same file the bot uses).
        if Path(".env").exists():
            load_dotenv(".env", override=False)
    except ImportError:
        pass

    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    app = create_app()
    print(f"Trading dashboard -> http://127.0.0.1:{port}  (local only, Ctrl+C to stop)")
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
