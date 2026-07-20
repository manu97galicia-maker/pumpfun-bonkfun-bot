"""Password auth for controlling the dashboard from outside the PC.

Localhost is always fully trusted (no login). Remote clients (e.g. the phone
over Tailscale) must log in with ``DASHBOARD_PASSWORD`` to reach the control
endpoints. Withdraw and wallet-import stay localhost-only regardless (handled
in server.py), so a stolen phone/password can't move funds.

The session cookie holds a token derived from the password, so no separate
secret store is needed. If ``DASHBOARD_PASSWORD`` is unset, remote control is
simply disabled (login always fails) and remote clients get read-only nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import os

COOKIE = "dash_auth"
_TOKEN_SALT = "sniper-deck::v1::"


def _user() -> str:
    return os.getenv("DASHBOARD_USER") or ""


def _password() -> str:
    return os.getenv("DASHBOARD_PASSWORD") or ""


def password_set() -> bool:
    """True if dashboard credentials (user + password) are configured."""
    return bool(_user() and _password())


def token() -> str | None:
    """Stable session token derived from the credentials (None if unset)."""
    user, pw = _user(), _password()
    if not (user and pw):
        return None
    return hashlib.sha256((_TOKEN_SALT + user + ":" + pw).encode()).hexdigest()


def check_password(user: str, candidate: str) -> bool:
    """Constant-time check of a submitted username + password."""
    real_user, real_pw = _user(), _password()
    if not (real_user and real_pw):
        return False
    ok_user = hmac.compare_digest(user or "", real_user)
    ok_pw = hmac.compare_digest(candidate or "", real_pw)
    return ok_user and ok_pw


def cookie_valid(cookie_value: str | None) -> bool:
    """True if the cookie matches the current token."""
    tok = token()
    return bool(tok) and bool(cookie_value) and hmac.compare_digest(cookie_value, tok)
