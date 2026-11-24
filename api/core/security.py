"""Security helpers: rate-limit, headers, password policy, client IP."""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from starlette.requests import Request
from starlette.responses import Response

# ── Password policy ─────────────────────────────────────────
MIN_PASSWORD_LENGTH = 10
_PASSWORD_HAS_LETTER = re.compile(r"[A-Za-z]")
_PASSWORD_HAS_DIGIT = re.compile(r"\d")


def validate_password_strength(password: str) -> Optional[str]:
    """Return error message or None if OK."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов"
    if not _PASSWORD_HAS_LETTER.search(password):
        return "Пароль должен содержать хотя бы одну букву"
    if not _PASSWORD_HAS_DIGIT.search(password):
        return "Пароль должен содержать хотя бы одну цифру"
    return None


# ── Login rate limit (in-memory, per process) ───────────────
_LOCK = threading.Lock()
_ATTEMPTS: Dict[str, Deque[float]] = defaultdict(deque)
_LOCKOUT_UNTIL: Dict[str, float] = {}

LOGIN_WINDOW_SEC = 15 * 60
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_SEC = 15 * 60


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _prune(bucket: Deque[float], now: float) -> None:
    while bucket and now - bucket[0] > LOGIN_WINDOW_SEC:
        bucket.popleft()


def check_login_rate(request: Request, username: str) -> Optional[str]:
    """If locked out / too many tries — return Russian error, else None."""
    key = f"{client_ip(request)}|{username.strip().lower()}"
    now = time.time()
    with _LOCK:
        until = _LOCKOUT_UNTIL.get(key, 0)
        if until > now:
            mins = max(1, int((until - now) / 60) + 1)
            return f"Слишком много попыток. Повторите через ~{mins} мин."
        bucket = _ATTEMPTS[key]
        _prune(bucket, now)
        if len(bucket) >= LOGIN_MAX_ATTEMPTS:
            _LOCKOUT_UNTIL[key] = now + LOGIN_LOCKOUT_SEC
            return "Слишком много попыток. Повторите через 15 мин."
    return None


def record_login_failure(request: Request, username: str) -> None:
    key = f"{client_ip(request)}|{username.strip().lower()}"
    now = time.time()
    with _LOCK:
        bucket = _ATTEMPTS[key]
        _prune(bucket, now)
        bucket.append(now)
        if len(bucket) >= LOGIN_MAX_ATTEMPTS:
            _LOCKOUT_UNTIL[key] = now + LOGIN_LOCKOUT_SEC


def clear_login_failures(request: Request, username: str) -> None:
    key = f"{client_ip(request)}|{username.strip().lower()}"
    with _LOCK:
        _ATTEMPTS.pop(key, None)
        _LOCKOUT_UNTIL.pop(key, None)


# ── Response hardening ──────────────────────────────────────

def apply_security_headers(response: Response, path: str = "") -> Response:
    """Defense-in-depth headers. CSP is report-friendly for dashboard scripts."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data: https://unpkg.com https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    if (
        path.startswith("/api/auth/")
        or path.startswith("/api/admin/")
        or path.startswith("/api/data-console/")
        or path.endswith("/data-console.html")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
    return response


def strip_sensitive_user(user: dict) -> dict:
    """Never leak password fields into JSON responses accidentally."""
    out = dict(user)
    for key in ("password_hash", "password_salt", "password", "current_password", "new_password"):
        out.pop(key, None)
    return out
