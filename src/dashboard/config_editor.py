"""Read and edit the strategy fields of a bot YAML config from the dashboard.

Only a whitelist of scalar keys under the ``trade:`` block is touched (TP, SL,
partial-sell %, slippage, exit strategy, timing). Edits are surgical
line-replacements so the hand-written comments and the rest of the file are
preserved; a commented-out key (e.g. ``#take_profit_percentage: 0.1``) is
uncommented and set.

Percentages are stored as fractions in the YAML (0.4 = 40%). The dashboard
converts to/from whole percents for display.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path("bots")

# Editable keys under `trade:` and their Python type for formatting.
# Take-profit is a 3-tier ladder (flattened to scalars so the panel can edit
# each field): tier 1 = take_profit_percentage / take_profit_sell_percentage,
# tiers 2 and 3 = tpN_gain / tpN_sell. `gain` is fraction above entry
# (x2 = 1.0, x5 = 4.0); `sell` is fraction of the original position.
STRATEGY_FIELDS: dict[str, type] = {
    "exit_strategy": str,
    "buy_amount": float,
    "buy_slippage": float,
    "sell_slippage": float,
    "take_profit_percentage": float,
    "take_profit_sell_percentage": float,
    "tp2_gain": float,
    "tp2_sell": float,
    "tp3_gain": float,
    "tp3_sell": float,
    "stop_loss_percentage": float,
    "max_hold_time": int,
    "price_check_interval": int,
}


def list_configs(config_dir: str | Path = CONFIG_DIR) -> list[str]:
    """Return the bot config filenames (``*.yaml``) in the configs dir."""
    return sorted(p.name for p in Path(config_dir).glob("*.yaml"))


def _safe_path(name: str, config_dir: str | Path = CONFIG_DIR) -> Path:
    """Resolve a config filename to a path inside the configs dir (only)."""
    path = (Path(config_dir) / name).resolve()
    root = Path(config_dir).resolve()
    if root not in path.parents or path.suffix not in (".yaml", ".yml"):
        msg = f"Invalid config file: {name}"
        raise ValueError(msg)
    return path


DEXSCREENER_FIELDS: dict[str, type] = {
    "enabled": bool,
    "enforce": bool,
    "block_on_error": bool,
    "require_dex_paid": bool,
    "require_listed": bool,
    "min_boosts": float,
    "min_liquidity_usd": float,
    "min_volume_h24": float,
    "min_market_cap": float,
    "max_market_cap": float,
    "require_liquidity_locked": bool,
    "min_lp_locked_pct": float,
    "timeout_seconds": int,
}


def read_strategy(name: str, config_dir: str | Path = CONFIG_DIR) -> dict[str, Any]:
    """Read the current strategy values from a bot config.

    Args:
        name: Config filename (e.g. ``bot-sniper-1-geyser.yaml``).
        config_dir: Directory holding the configs.

    Returns:
        ``{"name", "trade": {field: value}}`` with only the editable fields.
    """
    path = _safe_path(name, config_dir)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    trade = data.get("trade", {}) if isinstance(data, dict) else {}
    values = {k: trade.get(k) for k in STRATEGY_FIELDS}
    return {"name": name, "bot_name": data.get("name", name), "trade": values}


def _format_value(value: Any, kind: type) -> str:
    """Format a Python value for YAML output."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if kind is str:
        return f'"{value}"'
    if kind is int:
        return str(int(value))
    # float: trim trailing zeros but keep a leading digit
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _trade_block_bounds(lines: list[str]) -> tuple[int, int]:
    """Return (start, end) line indices of the `trade:` mapping block."""
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^trade:\s*(#.*)?$", line):
            start = i
            break
    if start is None:
        msg = "No `trade:` block found in config"
        raise ValueError(msg)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        # A new top-level key (no leading whitespace, not a comment/blank).
        if lines[j] and not lines[j][0].isspace() and not lines[j].lstrip().startswith("#"):
            end = j
            break
    return start, end


def update_strategy(
    name: str, updates: dict[str, Any], config_dir: str | Path = CONFIG_DIR
) -> dict[str, Any]:
    """Apply strategy updates to a bot config, preserving comments.

    Args:
        name: Config filename.
        updates: ``{field: value}`` for keys in :data:`STRATEGY_FIELDS`.
        config_dir: Directory holding the configs.

    Returns:
        The re-read strategy values after the update.

    Raises:
        ValueError: On an unknown field or a missing ``trade:`` block.
    """
    unknown = set(updates) - set(STRATEGY_FIELDS)
    if unknown:
        msg = f"Unknown strategy fields: {sorted(unknown)}"
        raise ValueError(msg)

    path = _safe_path(name, config_dir)
    lines = path.read_text(encoding="utf-8").splitlines()
    start, end = _trade_block_bounds(lines)

    # Indent used by existing trade children (default 2 spaces), and the index
    # just after the last real (indented, non-comment) child line so inserts
    # land inside the block, not after its trailing comments.
    indent = "  "
    insert_at = start + 1
    for k in range(start + 1, end):
        stripped = lines[k].strip()
        if lines[k][:1].isspace() and stripped and not stripped.startswith("#"):
            m = re.match(r"^(\s+)\S", lines[k])
            if m:
                indent = m.group(1)
            insert_at = k + 1

    to_insert: list[str] = []
    for key, value in updates.items():
        formatted = _format_value(value, STRATEGY_FIELDS[key])
        pattern = re.compile(
            rf"^(\s*)(#\s*)?{re.escape(key)}\s*:\s*([^#]*?)(\s+#.*)?$"
        )
        replaced = False
        for k in range(start + 1, end):
            m = pattern.match(lines[k])
            if m:
                comment = m.group(4) or ""
                lines[k] = f"{indent}{key}: {formatted}{comment}"
                replaced = True
                break
        if not replaced:
            to_insert.append(f"{indent}{key}: {formatted}")

    for offset, new_line in enumerate(to_insert):
        lines.insert(insert_at + offset, new_line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read_strategy(name, config_dir)


def set_enabled(
    name: str, enabled: bool, config_dir: str | Path = CONFIG_DIR
) -> bool:
    """Turn a bot config on/off via its top-level ``enabled`` key.

    Takes effect on the next bot start (configs are read at startup).
    """
    path = _safe_path(name, config_dir)
    lines = path.read_text(encoding="utf-8").splitlines()
    val = "true" if enabled else "false"
    done = False
    for i, line in enumerate(lines):
        m = re.match(r"^enabled\s*:\s*([^#]*?)(\s+#.*)?$", line)
        if m:
            comment = m.group(2) or ""
            lines[i] = f"enabled: {val}{comment}"
            done = True
            break
    if not done:
        lines.insert(0, f"enabled: {val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return enabled


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def update_dexscreener(
    name: str, updates: dict[str, Any], config_dir: str | Path = CONFIG_DIR
) -> dict[str, Any]:
    """Edit the ``filters.dexscreener`` block of a config (indent-aware).

    Args:
        name: Config filename.
        updates: ``{field: value}`` for keys in :data:`DEXSCREENER_FIELDS`.
        config_dir: Directory holding the configs.

    Returns:
        The re-read dexscreener values after the update.
    """
    unknown = set(updates) - set(DEXSCREENER_FIELDS)
    if unknown:
        msg = f"Unknown dexscreener fields: {sorted(unknown)}"
        raise ValueError(msg)

    path = _safe_path(name, config_dir)
    lines = path.read_text(encoding="utf-8").splitlines()

    header = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*dexscreener:\s*(#.*)?$", line):
            header = i
            break
    if header is None:
        msg = "No `dexscreener:` block found in config"
        raise ValueError(msg)

    header_indent = _indent_of(lines[header])
    end = len(lines)
    child_indent = header_indent + 2
    insert_at = header + 1
    for j in range(header + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent_of(lines[j]) <= header_indent:
            end = j
            break
        child_indent = _indent_of(lines[j])
        insert_at = j + 1
    pad = " " * child_indent

    to_insert: list[str] = []
    for key, value in updates.items():
        formatted = _format_value(value, DEXSCREENER_FIELDS[key])
        pattern = re.compile(
            rf"^(\s*)(#\s*)?{re.escape(key)}\s*:\s*([^#]*?)(\s+#.*)?$"
        )
        replaced = False
        for k in range(header + 1, end):
            m = pattern.match(lines[k])
            if m:
                comment = m.group(4) or ""
                lines[k] = f"{pad}{key}: {formatted}{comment}"
                replaced = True
                break
        if not replaced:
            to_insert.append(f"{pad}{key}: {formatted}")

    for offset, new_line in enumerate(to_insert):
        lines.insert(insert_at + offset, new_line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    dex = data.get("filters", {}).get("dexscreener", {})
    return {"name": name, "dexscreener": {k: dex.get(k) for k in DEXSCREENER_FIELDS}}
