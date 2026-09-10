"""Session auth for Asim GCP live desk (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Optional

AUTH_USER = os.environ.get("AUTH_USER", "admin")
AUTH_PASS = os.environ.get("AUTH_PASS", "changeme")
AUTH_SECRET = os.environ.get("AUTH_SECRET", "asim-gcp-dev-secret-change-on-deploy")
SESSION_COOKIE = "asim_session"
SESSION_HOURS = int(os.environ.get("AUTH_SESSION_HOURS", "24"))
REMEMBER_DAYS = int(os.environ.get("AUTH_REMEMBER_DAYS", "30"))


def _sign(payload: str) -> str:
    return hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token(username: str, remember: bool = False) -> tuple[str, int]:
    ttl = REMEMBER_DAYS * 86400 if remember else SESSION_HOURS * 3600
    exp = int(time.time()) + ttl
    payload = f"{username}:{exp}"
    token = base64.urlsafe_b64encode(f"{payload}:{_sign(payload)}".encode()).decode()
    return token, ttl


def verify_session_token(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit(":", 1)
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        username, exp_s = payload.split(":", 1)
        if int(exp_s) < int(time.time()):
            return None
        if username != AUTH_USER:
            return None
        return username
    except (ValueError, UnicodeDecodeError):
        return None


def check_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, AUTH_USER) and hmac.compare_digest(password, AUTH_PASS)
