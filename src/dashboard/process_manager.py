"""Start/stop the trading bot as a child process from the dashboard.

The dashboard is a control panel: this manages a single bot process
(``src/bot_runner.py``) so a START/STOP button can launch or halt real
trading. Launching trades real SOL, so callers should confirm first and the
endpoints are localhost-only (see server.py).

Notes:
    - Uses the same interpreter that runs the dashboard (``sys.executable``),
      so run the dashboard from the project's uv/venv Python.
    - The bot may spawn one child process per enabled bot config
      (``separate_process: true``), so STOP kills the whole process tree
      (taskkill /T on Windows, process-group signal elsewhere).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# src/dashboard/process_manager.py -> parents[1] = src, parents[2] = repo root
SRC_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_ENTRY = SRC_DIR / "bot_runner.py"


class BotProcess:
    """Owns at most one running bot subprocess and reports its status."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None

    def status(self) -> dict[str, Any]:
        """Current bot process status."""
        running = self._proc is not None and self._proc.poll() is None
        exit_code = None
        if self._proc is not None and not running:
            exit_code = self._proc.returncode
        uptime = None
        if running and self._started_at is not None:
            uptime = round(time.monotonic() - self._started_at, 1)
        return {
            "running": running,
            "pid": self._proc.pid if running else None,
            "uptime_seconds": uptime,
            "last_exit_code": exit_code,
        }

    def start(self) -> dict[str, Any]:
        """Launch the bot if not already running. Returns status."""
        if self._proc is not None and self._proc.poll() is None:
            return {**self.status(), "message": "already running"}
        if not BOT_ENTRY.exists():
            return {**self.status(), "error": f"bot entry not found: {BOT_ENTRY}"}

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, "-u", str(BOT_ENTRY)]
        kwargs: dict[str, Any] = {"cwd": str(REPO_ROOT), "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        self._proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
        self._started_at = time.monotonic()
        return {**self.status(), "message": "started"}

    def stop(self) -> dict[str, Any]:
        """Stop the bot and its child processes. Returns status."""
        if self._proc is None or self._proc.poll() is not None:
            self._proc = None
            return {"running": False, "message": "not running"}

        pid = self._proc.pid
        try:
            if os.name == "nt":
                subprocess.run(  # noqa: S603, S607
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            pass

        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        finally:
            self._proc = None
            self._started_at = None
        return {"running": False, "message": "stopped"}
