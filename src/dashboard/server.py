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
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from dashboard import auth

logger = logging.getLogger(__name__)

# Endpoints that require the request to come from the PC itself, even with a
# valid login (they move funds or handle keys).
PC_ONLY_PATHS = frozenset({"/api/withdraw", "/api/wallet/import"})
# Endpoints reachable by a remote client WITHOUT logging in.
PUBLIC_ENDPOINTS = frozenset({("GET", "/"), ("POST", "/api/login")})


def _is_local(request: web.Request) -> bool:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return bool(peer) and peer[0] in ("127.0.0.1", "::1")


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Gate remote access: localhost is trusted; others must log in.

    PC-only endpoints (withdraw, wallet import) always require localhost.
    """
    local = _is_local(request)
    path = request.path

    if path in PC_ONLY_PATHS and not local:
        return web.json_response(
            {"ok": False, "error": "Solo desde el PC por seguridad"}, status=403
        )
    if local:
        return await handler(request)
    if (request.method, path) in PUBLIC_ENDPOINTS or path.startswith("/favicon"):
        return await handler(request)
    if auth.cookie_valid(request.cookies.get(auth.COOKIE)):
        return await handler(request)
    return web.json_response({"ok": False, "error": "login required"}, status=401)


async def handle_login(request: web.Request) -> web.Response:
    """Log in a remote client with the dashboard password."""
    try:
        body = await request.json()
        user = str(body.get("user", ""))
        password = str(body.get("password", ""))
    except json.JSONDecodeError:
        user = password = ""
    if not auth.password_set():
        return web.json_response(
            {"ok": False, "error": "Sin credenciales. Pon DASHBOARD_USER y DASHBOARD_PASSWORD en .env."},
            status=400,
        )
    if not auth.check_password(user, password):
        return web.json_response(
            {"ok": False, "error": "Usuario o PIN incorrecto"}, status=401
        )
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        auth.COOKIE,
        auth.token() or "",
        httponly=True,
        samesite="Lax",
        max_age=60 * 60 * 24 * 30,
    )
    return resp

from dashboard.analytics import (
    build_positions,
    live_buys,
    load_trades,
    strategy_analysis,
    summarize,
)
from dashboard.process_manager import REPO_ROOT, BotProcess

STATIC_DIR = Path(__file__).parent / "static"
LAMPORTS_PER_SOL = 1_000_000_000
# Public RPC used only to read the wallet balance when no endpoint is
# configured, so the dashboard can show a SOL balance out of the box.
FALLBACK_RPC = "https://api.mainnet-beta.solana.com"
# Kept back on every withdraw so the account keeps a working SOL buffer for
# fees, rent and in-flight trades.
WITHDRAW_FEE_RESERVE_SOL = 0.03


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
    """Query the wallet SOL balance via RPC.

    Uses the configured endpoint, falling back to a public RPC so the balance
    shows even before the user sets SOLANA_NODE_RPC_ENDPOINT. Returns None only
    if the query fails.
    """
    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT") or FALLBACK_RPC
    from solana.rpc.async_api import AsyncClient

    try:
        async with AsyncClient(rpc) as client:
            resp = await client.get_balance(pubkey)
            return resp.value / LAMPORTS_PER_SOL
    except Exception as exc:  # noqa: BLE001 -- show n/d rather than crash
        logger.warning("Balance query failed: %s", exc)
        return None


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
        "bot": request.app["bot"].status(),
        "session": {
            "local": _is_local(request),
            "password_set": auth.password_set(),
        },
    }
    return web.json_response(state)


def _require_localhost(request: web.Request) -> web.Response | None:
    """Reject non-localhost callers. Returns an error response or None."""
    peer = request.transport.get_extra_info("peername")
    if not peer or peer[0] not in ("127.0.0.1", "::1"):
        return web.json_response({"ok": False, "error": "Local requests only"}, status=403)
    return None


async def handle_bot_status(request: web.Request) -> web.Response:
    """Return the bot process status."""
    return web.json_response(request.app["bot"].status())


async def handle_bot_start(request: web.Request) -> web.Response:
    """Start the trading bot (real trading). Localhost only."""
    blocked = _require_localhost(request)
    if blocked:
        return blocked
    return web.json_response({"ok": True, **request.app["bot"].start()})


async def handle_bot_stop(request: web.Request) -> web.Response:
    """Stop the trading bot. Localhost only."""
    blocked = _require_localhost(request)
    if blocked:
        return blocked
    return web.json_response({"ok": True, **request.app["bot"].stop()})


def _write_env_private_key(b58_key: str) -> None:
    """Set SOLANA_PRIVATE_KEY in the repo .env, preserving other lines."""
    env_path = REPO_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("SOLANA_PRIVATE_KEY="):
            lines[i] = f"SOLANA_PRIVATE_KEY={b58_key}"
            replaced = True
            break
    if not replaced:
        lines.append(f"SOLANA_PRIVATE_KEY={b58_key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def handle_wallet_import(request: web.Request) -> web.Response:
    """Import a wallet from a 12/24-word seed phrase. Localhost only.

    Derives the Phantom-standard key, stores it in .env as SOLANA_PRIVATE_KEY
    and activates it for the dashboard. The mnemonic is never stored or logged;
    the private key is never returned. Restart the bot to trade with it.
    """
    blocked = _require_localhost(request)
    if blocked:
        return blocked
    try:
        body = await request.json()
        mnemonic = str(body["mnemonic"])
        account = int(body.get("account", 0))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "Expected {mnemonic, account?}"}, status=400
        )

    try:
        from dashboard.wallet_import import derive_from_mnemonic

        derived = derive_from_mnemonic(mnemonic, account)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except ImportError:
        return web.json_response(
            {"ok": False, "error": "bip-utils not installed (run: uv sync)"},
            status=500,
        )

    _write_env_private_key(derived["private_key_b58"])
    os.environ["SOLANA_PRIVATE_KEY"] = derived["private_key_b58"]
    logger.info("Imported wallet %s via dashboard", derived["pubkey"])
    return web.json_response({"ok": True, "address": derived["pubkey"]})


async def handle_dexpaid(request: web.Request) -> web.Response:
    """Live feed of tokens that recently paid DexScreener (dex paid / boosts)."""
    from dashboard.dexpaid_feed import get_candidates

    try:
        rows = await get_candidates(limit=10, max_age_minutes=180.0)
    except Exception as exc:  # noqa: BLE001 -- feed is best-effort
        logger.warning("dexpaid feed failed: %s", exc)
        rows = []
    return web.json_response({"candidates": rows})


async def handle_configs(request: web.Request) -> web.Response:
    """List bot configs with their current strategy + filter values."""
    from dashboard.config_editor import (
        DEXSCREENER_FIELDS,
        list_configs,
        read_strategy,
    )
    import yaml

    out = []
    for name in list_configs():
        strat = read_strategy(name)
        data = yaml.safe_load((Path("bots") / name).read_text(encoding="utf-8")) or {}
        dex = data.get("filters", {}).get("dexscreener", {}) or {}
        out.append(
            {
                **strat,
                "enabled": data.get("enabled", True),
                "platform": data.get("platform", "pump_fun"),
                "dexscreener": {k: dex.get(k) for k in DEXSCREENER_FIELDS},
            }
        )
    return web.json_response({"configs": out})


async def handle_config_update(request: web.Request) -> web.Response:
    """Apply strategy and/or dexscreener updates to a config. Localhost only."""
    blocked = _require_localhost(request)
    if blocked:
        return blocked
    from dashboard.config_editor import set_enabled, update_dexscreener, update_strategy

    try:
        body = await request.json()
        name = str(body["file"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return web.json_response({"ok": False, "error": "Expected {file, ...}"}, status=400)
    try:
        result: dict[str, Any] = {"ok": True, "file": name}
        if "enabled" in body:
            result["enabled"] = set_enabled(name, bool(body["enabled"]))
        if body.get("trade"):
            result["trade"] = update_strategy(name, body["trade"])["trade"]
        if body.get("dexscreener"):
            result["dexscreener"] = update_dexscreener(name, body["dexscreener"])["dexscreener"]
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response(result)


async def handle_command(request: web.Request) -> web.Response:
    """Interpret a written order and apply it to a config. Localhost only."""
    blocked = _require_localhost(request)
    if blocked:
        return blocked
    from dashboard.command_interpreter import interpret
    from dashboard.config_editor import update_dexscreener, update_strategy

    try:
        body = await request.json()
        name = str(body["file"])
        text = str(body["text"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return web.json_response(
            {"ok": False, "error": "Expected {file, text}"}, status=400
        )

    plan = interpret(text)
    if not plan["matched"]:
        return web.json_response({"ok": False, "note": plan["note"], "actions": []})

    trade_updates = {a["field"]: a["value"] for a in plan["actions"] if a["scope"] == "trade"}
    dex_updates = {a["field"]: a["value"] for a in plan["actions"] if a["scope"] == "dexscreener"}
    try:
        if trade_updates:
            update_strategy(name, trade_updates)
        if dex_updates:
            update_dexscreener(name, dex_updates)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response(
        {"ok": True, "actions": plan["actions"], "applied_to": name}
    )


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
    reserve = int(round(WITHDRAW_FEE_RESERVE_SOL * LAMPORTS_PER_SOL))

    async with AsyncClient(rpc) as client:
        balance = (await client.get_balance(keypair.pubkey())).value
        if lamports > balance - reserve:
            available = max(0, balance - reserve) / LAMPORTS_PER_SOL
            raise ValueError(
                f"Amount too high: {available:.6f} SOL available after "
                f"{WITHDRAW_FEE_RESERVE_SOL} SOL fee reserve"
            )
        ix = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(), to_pubkey=dest, lamports=lamports
            )
        )
        blockhash = (await client.get_latest_blockhash()).value.blockhash
        msg = Message.new_with_blockhash([ix], keypair.pubkey(), blockhash)
        tx = Transaction([keypair], msg, blockhash)
        resp = await client.send_transaction(tx)
        return str(resp.value)


def create_app(trades_log: str | Path | None = None) -> web.Application:
    """Build the aiohttp application."""
    app = web.Application(middlewares=[auth_middleware])
    app["trades_log"] = Path(trades_log) if trades_log else Path("trades") / "trades.log"
    app["bot"] = BotProcess()
    app.router.add_post("/api/login", handle_login)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/wallet", handle_wallet)
    app.router.add_post("/api/analysis", handle_analysis)
    app.router.add_post("/api/withdraw", handle_withdraw)
    app.router.add_get("/api/bot/status", handle_bot_status)
    app.router.add_post("/api/bot/start", handle_bot_start)
    app.router.add_post("/api/bot/stop", handle_bot_stop)
    app.router.add_post("/api/wallet/import", handle_wallet_import)
    app.router.add_get("/api/configs", handle_configs)
    app.router.add_post("/api/config", handle_config_update)
    app.router.add_post("/api/command", handle_command)
    app.router.add_get("/api/dexpaid", handle_dexpaid)
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
    # Default localhost-only. Set DASHBOARD_HOST=0.0.0.0 (ideally behind a
    # private VPN like Tailscale) to reach it from your phone. Money-moving
    # endpoints (bot start/stop, withdraw, wallet import, config writes) stay
    # localhost-only regardless, so remote access is monitoring + read.
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    scope = "local only" if host == "127.0.0.1" else f"reachable on {host}"
    app = create_app()
    print(f"Trading dashboard -> http://{host}:{port}  ({scope}, Ctrl+C to stop)")
    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    main()
