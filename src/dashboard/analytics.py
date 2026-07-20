"""Analytics for the trading dashboard.

Pure functions (no network, no I/O beyond reading the trade log) that turn the
bot's ``trades/trades.log`` JSON-lines into the shapes the dashboard needs:
open positions, closed trades, aggregate results and strategy analysis.

Everything is *derived* from the trade log the bot already writes in
``UniversalTrader._log_trade`` (fields: timestamp, action, platform,
token_address, symbol, price, amount, tx_hash). ``price`` is SOL per token and
``amount`` is the token quantity, so ``price * amount`` is the SOL value of the
leg. P&L is therefore an estimate from logged fills, not an on-chain audit.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_TRADES_LOG = Path("trades") / "trades.log"


def load_trades(path: str | Path = DEFAULT_TRADES_LOG) -> list[dict[str, Any]]:
    """Load and parse the trade log into a list of event dicts.

    Malformed lines are skipped rather than raising, so a partially-written
    log (the bot may be appending concurrently) never breaks the dashboard.

    Args:
        path: Path to the JSON-lines trade log.

    Returns:
        Trade events in file order (oldest first). Empty list if missing.
    """
    log_path = Path(path)
    if not log_path.exists():
        return []

    trades: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("action"):
            trades.append(event)
    return trades


def _leg_value_sol(event: dict[str, Any]) -> float:
    """SOL value of a single trade leg (price per token * token amount)."""
    price = _num(event.get("price"))
    amount = _num(event.get("amount"))
    if price is None or amount is None:
        return 0.0
    return price * amount


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp, tolerating a trailing 'Z'."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_positions(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Collapse the trade log into per-token positions.

    A token is *closed* once it has at least one sell (the bot exits the full
    position), otherwise it is *open*. Grouping is by ``token_address``.

    Args:
        trades: Events from :func:`load_trades`.

    Returns:
        ``{"open": [...], "closed": [...]}`` where each item carries symbol,
        platform, invested/returned SOL, realized P&L and timing.
    """
    by_token: dict[str, dict[str, Any]] = {}

    for event in trades:
        mint = event.get("token_address")
        if not mint:
            continue
        agg = by_token.setdefault(
            mint,
            {
                "mint": mint,
                "symbol": event.get("symbol") or "?",
                "platform": event.get("platform") or "pump_fun",
                "invested_sol": 0.0,
                "returned_sol": 0.0,
                "qty_bought": 0.0,
                "qty_sold": 0.0,
                "n_buys": 0,
                "n_sells": 0,
                "entry_time": None,
                "exit_time": None,
                "last_tx": event.get("tx_hash"),
            },
        )
        agg["symbol"] = event.get("symbol") or agg["symbol"]
        if event.get("platform"):
            agg["platform"] = event["platform"]
        agg["last_tx"] = event.get("tx_hash") or agg["last_tx"]

        value = _leg_value_sol(event)
        ts = _parse_ts(event.get("timestamp"))
        action = event.get("action")

        if action == "buy":
            agg["invested_sol"] += value
            agg["qty_bought"] += _num(event.get("amount")) or 0.0
            agg["n_buys"] += 1
            if ts and (agg["entry_time"] is None or ts < agg["entry_time"]):
                agg["entry_time"] = ts
        elif action == "sell":
            agg["returned_sol"] += value
            agg["qty_sold"] += _num(event.get("amount")) or 0.0
            agg["n_sells"] += 1
            if ts and (agg["exit_time"] is None or ts > agg["exit_time"]):
                agg["exit_time"] = ts

    open_positions: list[dict[str, Any]] = []
    closed_positions: list[dict[str, Any]] = []

    for agg in by_token.values():
        is_open = agg["n_sells"] == 0
        realized = agg["returned_sol"] - agg["invested_sol"]
        pnl_pct = (
            (realized / agg["invested_sol"] * 100.0)
            if agg["invested_sol"] > 0
            else 0.0
        )
        hold_seconds = None
        if agg["entry_time"] and agg["exit_time"]:
            hold_seconds = (agg["exit_time"] - agg["entry_time"]).total_seconds()

        record = {
            "mint": agg["mint"],
            "symbol": agg["symbol"],
            "platform": agg["platform"],
            "invested_sol": round(agg["invested_sol"], 9),
            "returned_sol": round(agg["returned_sol"], 9),
            "realized_pnl_sol": round(realized, 9),
            "realized_pnl_pct": round(pnl_pct, 2),
            "qty_bought": agg["qty_bought"],
            "qty_sold": agg["qty_sold"],
            "n_buys": agg["n_buys"],
            "n_sells": agg["n_sells"],
            "entry_time": agg["entry_time"].isoformat() if agg["entry_time"] else None,
            "exit_time": agg["exit_time"].isoformat() if agg["exit_time"] else None,
            "hold_seconds": hold_seconds,
            "last_tx": agg["last_tx"],
            "status": "open" if is_open else "closed",
        }
        (open_positions if is_open else closed_positions).append(record)

    open_positions.sort(key=lambda r: r["entry_time"] or "", reverse=True)
    closed_positions.sort(key=lambda r: r["exit_time"] or "", reverse=True)
    return {"open": open_positions, "closed": closed_positions}


def summarize(positions: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Aggregate closed positions into the "current results" headline block.

    Args:
        positions: Output of :func:`build_positions`.

    Returns:
        Totals, win rate, averages and best/worst closed trade.
    """
    closed = positions["closed"]
    open_ = positions["open"]

    total_realized = sum(p["realized_pnl_sol"] for p in closed)
    total_invested = sum(p["invested_sol"] for p in closed)
    wins = [p for p in closed if p["realized_pnl_sol"] > 0]
    losses = [p for p in closed if p["realized_pnl_sol"] < 0]
    n_closed = len(closed)

    best = max(closed, key=lambda p: p["realized_pnl_sol"], default=None)
    worst = min(closed, key=lambda p: p["realized_pnl_sol"], default=None)
    open_invested = sum(p["invested_sol"] for p in open_)

    return {
        "total_realized_sol": round(total_realized, 9),
        "total_invested_sol": round(total_invested, 9),
        "roi_pct": round(total_realized / total_invested * 100.0, 2)
        if total_invested > 0
        else 0.0,
        "avg_pnl_per_coin_sol": round(total_realized / n_closed, 9)
        if n_closed
        else 0.0,
        "win_rate_pct": round(len(wins) / n_closed * 100.0, 2) if n_closed else 0.0,
        "wins": len(wins),
        "losses": len(losses),
        "closed_count": n_closed,
        "open_count": len(open_),
        "open_invested_sol": round(open_invested, 9),
        "best": best,
        "worst": worst,
    }


def live_buys(
    trades: list[dict[str, Any]], limit: int = 30
) -> list[dict[str, Any]]:
    """Most recent buy events, newest first, for the live feed.

    Args:
        trades: Events from :func:`load_trades`.
        limit: Max rows to return.

    Returns:
        Recent buys with symbol, mint, SOL spent, price, tx and timestamp.
    """
    buys = [
        {
            "timestamp": e.get("timestamp"),
            "symbol": e.get("symbol") or "?",
            "mint": e.get("token_address"),
            "platform": e.get("platform") or "pump_fun",
            "price": _num(e.get("price")),
            "amount": _num(e.get("amount")),
            "spent_sol": round(_leg_value_sol(e), 9),
            "tx_hash": e.get("tx_hash"),
        }
        for e in trades
        if e.get("action") == "buy"
    ]
    buys.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return buys[:limit]


def strategy_analysis(
    trades: list[dict[str, Any]], params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Study realized results under a strategy assumption.

    The headline use case: "average profitability per coin after paying a
    DexScreener boost". Pass ``cost_per_coin_sol`` (the boost cost) and it is
    subtracted from each closed coin's realized P&L before aggregating.

    Supported params (all optional):
        cost_per_coin_sol: Fixed cost subtracted from each closed coin
            (e.g. a DexScreener boost). Default 0.
        platform: Only consider this platform (e.g. ``pump_fun``).
        min_invested_sol: Ignore coins that invested less than this.
        label: Free-text label echoed back (e.g. "dex boost 500").

    Args:
        trades: Events from :func:`load_trades`.
        params: Strategy parameters.

    Returns:
        Per-coin net rows plus aggregate net metrics.
    """
    params = params or {}
    cost = _num(params.get("cost_per_coin_sol")) or 0.0
    platform = params.get("platform")
    min_invested = _num(params.get("min_invested_sol")) or 0.0

    positions = build_positions(trades)
    closed = positions["closed"]
    if platform:
        closed = [p for p in closed if p["platform"] == platform]
    if min_invested > 0:
        closed = [p for p in closed if p["invested_sol"] >= min_invested]

    rows: list[dict[str, Any]] = []
    for p in closed:
        net = p["realized_pnl_sol"] - cost
        rows.append(
            {
                "symbol": p["symbol"],
                "mint": p["mint"],
                "platform": p["platform"],
                "invested_sol": p["invested_sol"],
                "realized_pnl_sol": p["realized_pnl_sol"],
                "cost_sol": round(cost, 9),
                "net_pnl_sol": round(net, 9),
                "hold_seconds": p["hold_seconds"],
            }
        )

    n = len(rows)
    total_net = sum(r["net_pnl_sol"] for r in rows)
    total_gross = sum(r["realized_pnl_sol"] for r in rows)
    wins = [r for r in rows if r["net_pnl_sol"] > 0]
    holds = [r["hold_seconds"] for r in rows if r["hold_seconds"] is not None]

    return {
        "label": params.get("label") or "strategy",
        "cost_per_coin_sol": round(cost, 9),
        "platform": platform or "all",
        "coins": n,
        "avg_net_pnl_per_coin_sol": round(total_net / n, 9) if n else 0.0,
        "avg_gross_pnl_per_coin_sol": round(total_gross / n, 9) if n else 0.0,
        "total_net_pnl_sol": round(total_net, 9),
        "total_gross_pnl_sol": round(total_gross, 9),
        "total_cost_sol": round(cost * n, 9),
        "win_rate_pct": round(len(wins) / n * 100.0, 2) if n else 0.0,
        "avg_hold_seconds": round(sum(holds) / len(holds), 1) if holds else None,
        "profitable_after_cost": total_net > 0,
        "rows": sorted(rows, key=lambda r: r["net_pnl_sol"], reverse=True),
    }


def _num(value: Any) -> float | None:
    """Best-effort float conversion; None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
