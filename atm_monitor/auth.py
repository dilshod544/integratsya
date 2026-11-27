import base64
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import pyotp
import requests

from .config import settings

logger = logging.getLogger("atm_monitor.auth")

# После "too many login" не долбим BTech соседними TOTP-кодами.
_LOGIN_COOLDOWN_SEC = 15 * 60

LOGIN_REQUIRED = "BTech login required"

# Похоже на JWT: три base64url-блока через точку.
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")


def _trusted_unix_time() -> float:
    """Время «как у сети», а не обязательно локальные часы банк-сервера.

    Берём Date из ответа BTech (или запасной HTTPS-источник). Если не удалось —
    локальное time.time(). Так TOTP совпадает даже при сбитых часах ОС.
    """
    from email.utils import parsedate_to_datetime

    local = time.time()
    candidates = [
        settings.API_BASE_URL.rstrip("/") + "/",
        settings.API_BASE_URL.rstrip("/") + settings.LOGIN_PATH,
        "https://www.google.com/",
    ]
    for url in candidates:
        try:
            resp = requests.head(url, timeout=8, allow_redirects=True)
            if "Date" not in resp.headers:
                resp = requests.get(url, timeout=8, allow_redirects=True)
            date_hdr = resp.headers.get("Date")
            if not date_hdr:
                continue
            remote = parsedate_to_datetime(date_hdr).timestamp()
            skew = remote - local
            if abs(skew) > 2:
                logger.warning(
                    "Часы сервера отличаются от сети на %.0f сек — TOTP считаем по сетевому времени.",
                    skew,
                )
            return remote
        except Exception as e:
            logger.debug("Не удалось взять сетевое время с %s: %s", url, e)
    return local


def _totp_codes(secret: str) -> list:
    """1–3 кода вокруг сетевого времени (без большого перебора — иначе rate-limit)."""
    totp = pyotp.TOTP(secret)
    base = int(_trusted_unix_time())
    seen = set()
    codes = []
    for t in (0, -30, 30):
        c = totp.at(base + t)
        if c not in seen:
            seen.add(c)
            codes.append(c)
    return codes


def _find_jwt_recursive(node, _depth: int = 0):
    """Резервный способ найти токен: если настроенный LOGIN_TOKEN_RESPONSE_FIELD
    не совпал с реальным ответом API, ищем в ответе любую строку, похожую на JWT,
    на случай если сервер называет поле как-то иначе (accessToken, jwt, data.token...)."""
    if _depth > 4:
        return None
    if isinstance(node, str) and _JWT_RE.match(node):
        return node
    if isinstance(node, dict):
        for v in node.values():
            found = _find_jwt_recursive(v, _depth + 1)
            if found:
                return found
    if isinstance(node, list):
        for v in node:
            found = _find_jwt_recursive(v, _depth + 1)
            if found:
                return found
    return None


# Для заголовков и кук допускаем префикс вроде "Bearer " перед токеном.
_JWT_SEARCH_RE = re.compile(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")


def _find_jwt_in_headers(headers) -> "str | None":
    """Ищем JWT прямо в заголовках ответа (например X-Token, Authorization)
    и в Set-Cookie — некоторые API отдают токен не в теле, а в заголовке/куке."""
    for name, value in headers.items():
        match = _JWT_SEARCH_RE.search(value)
        if match:
            logger.info("Нашли токен в заголовке ответа '%s'.", name)
            return match.group(0)
    return None


def _format_btech_errors(err: dict) -> str:
    errors = err.get("errors") if isinstance(err, dict) else None
    if isinstance(errors, dict) and errors:
        parts = []
        for key, vals in errors.items():
            if isinstance(vals, list):
                parts.append(f"{key}: {', '.join(str(v) for v in vals)}")
            else:
                parts.append(f"{key}: {vals}")
        return "; ".join(parts)
    return (err.get("raw") if isinstance(err, dict) else None) or str(err)[:300]


class TokenManager:
    """Держит Bearer-токен BTech: ручная сессия (data-console) или авто-логин из .env.

    Приоритет:
    1) валидный JWT в памяти / PostgreSQL (btech_session);
    2) авто-логин с AUTH_OTP_SECRET, если задан;
    3) иначе — LOGIN_REQUIRED (без долбежки BTech).
    """

    def __init__(self):
        self._token = None
        self._exp = 0.0
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._loaded_from_db = False

    def has_valid_session(self) -> bool:
        with self._lock:
            self._ensure_loaded_locked()
            return bool(self._token and time.time() < self._exp - 60)

    def session_expires_at(self) -> "float | None":
        with self._lock:
            self._ensure_loaded_locked()
            if self._token and self._exp > time.time():
                return float(self._exp)
            return None

    def set_session(self, token: str) -> float:
        """Сохранить JWT (память + БД). Возвращает unix exp."""
        exp = self._decode_exp(token)
        with self._lock:
            self._token = token
            self._exp = exp
            self._cooldown_until = 0.0
            self._persist_locked()
        logger.info("BTech сессия сохранена, истекает: %s", time.ctime(exp))
        return exp

    def clear_session(self) -> None:
        with self._lock:
            self._token = None
            self._exp = 0.0
            self._delete_persisted_locked()
        logger.info("BTech сессия очищена")

    def get_token(self) -> str:
        with self._lock:
            self._ensure_loaded_locked()
            if self._token and time.time() <= self._exp - 60:
                return self._token
            if settings.AUTH_OTP_SECRET and settings.AUTH_USERNAME and settings.AUTH_PASSWORD:
                self._login_locked()
                return self._token
            self._token = None
            self._exp = 0.0
            raise RuntimeError(
                f"{LOGIN_REQUIRED} — войдите в BTech через Модуль данных "
                "(email / пароль / OTP)."
            )

    def force_refresh(self) -> str:
        """После 401: сбрасываем сессию; авто-логин только при AUTH_OTP_SECRET."""
        with self._lock:
            self._token = None
            self._exp = 0.0
            self._delete_persisted_locked()
            if settings.AUTH_OTP_SECRET and settings.AUTH_USERNAME and settings.AUTH_PASSWORD:
                self._login_locked()
                return self._token
            raise RuntimeError(
                f"{LOGIN_REQUIRED} — сессия истекла, войдите снова через Модуль данных."
            )

    def login_manual(self, email: str, password: str, otp_code: str) -> float:
        """Одна попытка логина с OTP из формы. JWT в ответ не отдаём — только exp."""
        email = (email or "").strip()
        password = password or ""
        otp_code = (otp_code or "").strip()
        if not email or not password or not otp_code:
            raise RuntimeError("Укажите email, пароль и OTP-код")

        with self._lock:
            now = time.time()
            if now < self._cooldown_until:
                wait = int(self._cooldown_until - now)
                raise RuntimeError(
                    f"BTech login cooldown ({wait}s) — подождите, чтобы не усилить блокировку."
                )

            url = settings.API_BASE_URL.rstrip("/") + settings.LOGIN_PATH
            body = {
                settings.LOGIN_USERNAME_FIELD: email,
                settings.LOGIN_PASSWORD_FIELD: password,
                settings.LOGIN_OTP_FIELD: otp_code,
            }
            logger.info("Ручной логин BTech (%s)...", url)
            resp = requests.post(
                url,
                json=body,
                params={"lang": settings.QUERY_PARAMS.get("lang", "ru")},
                timeout=settings.REQUEST_TIMEOUT,
            )
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                data = {"raw": (resp.text or "")[:300]}

            if not resp.ok:
                errors = (data.get("errors") or {}) if isinstance(data, dict) else {}
                if "password" in errors and any(
                    "many" in str(x).lower() for x in errors["password"]
                ):
                    self._cooldown_until = time.time() + _LOGIN_COOLDOWN_SEC
                    raise RuntimeError(
                        f"BTech login rate-limited — пауза {_LOGIN_COOLDOWN_SEC // 60} мин"
                    )
                raise RuntimeError(_format_btech_errors(data if isinstance(data, dict) else {"raw": str(data)}))

            token = self._extract_token(data)
            if not token:
                token = _find_jwt_recursive(data)
            if not token:
                token = _find_jwt_in_headers(resp.headers)
            if not token:
                raise RuntimeError("BTech не вернул токен в ответе логина")

            self._token = token
            self._exp = self._decode_exp(token)
            self._cooldown_until = 0.0
            self._persist_locked()
            logger.info("Ручной логин OK, истекает: %s", time.ctime(self._exp))
            return float(self._exp)

    def _ensure_loaded_locked(self) -> None:
        if self._loaded_from_db:
            return
        self._loaded_from_db = True
        try:
            from .db import connect

            with connect() as conn:
                row = conn.execute(
                    "SELECT token, EXTRACT(EPOCH FROM exp) AS exp FROM btech_session WHERE id = 1"
                ).fetchone()
            if row and row.get("token") and row.get("exp"):
                exp = float(row["exp"])
                if exp > time.time() + 60:
                    self._token = row["token"]
                    self._exp = exp
                    logger.info("BTech сессия загружена из БД, истекает: %s", time.ctime(exp))
        except Exception as e:  # noqa: BLE001
            logger.debug("Не удалось загрузить btech_session: %s", e)

    def _persist_locked(self) -> None:
        try:
            from .db import connect

            exp_dt = datetime.fromtimestamp(self._exp, tz=timezone.utc)
            with connect() as conn:
                conn.execute(
                    """INSERT INTO btech_session (id, token, exp, updated_at)
                       VALUES (1, %s, %s, now())
                       ON CONFLICT (id) DO UPDATE
                         SET token = EXCLUDED.token,
                             exp = EXCLUDED.exp,
                             updated_at = now()""",
                    (self._token, exp_dt),
                )
            self._loaded_from_db = True
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось сохранить btech_session")

    def _delete_persisted_locked(self) -> None:
        try:
            from .db import connect

            with connect() as conn:
                conn.execute("DELETE FROM btech_session WHERE id = 1")
            self._loaded_from_db = True
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось удалить btech_session", exc_info=True)

    def _login_locked(self):
        if not settings.AUTH_USERNAME or not settings.AUTH_PASSWORD:
            raise RuntimeError(
                "AUTH_USERNAME / AUTH_PASSWORD не заданы (см. .env). "
                "Без учётных данных сервис не может залогиниться сам."
            )

        now = time.time()
        if now < self._cooldown_until:
            wait = int(self._cooldown_until - now)
            raise RuntimeError(
                f"BTech login cooldown ({wait}s) — слишком много попыток входа. "
                f"Подождите, чтобы не усилить блокировку OTP."
            )

        url = settings.API_BASE_URL.rstrip("/") + settings.LOGIN_PATH
        payload = {
            settings.LOGIN_USERNAME_FIELD: settings.AUTH_USERNAME,
            settings.LOGIN_PASSWORD_FIELD: settings.AUTH_PASSWORD,
        }
        if settings.AUTH_OTP_SECRET:
            logger.info("Логинимся на платформу (%s) с TOTP (время — сетевое)...", url)
            codes = _totp_codes(settings.AUTH_OTP_SECRET)
        else:
            logger.info("Логинимся на платформу (%s)...", url)
            codes = [None]

        last_resp = None
        data = None
        rate_limited = False
        for code in codes:
            body = dict(payload)
            if code is not None:
                body[settings.LOGIN_OTP_FIELD] = code
            last_resp = requests.post(
                url,
                json=body,
                params={"lang": settings.QUERY_PARAMS.get("lang", "ru")},
                timeout=settings.REQUEST_TIMEOUT,
            )
            if last_resp.ok:
                data = last_resp.json()
                break
            try:
                err = last_resp.json()
            except Exception:
                err = {"raw": (last_resp.text or "")[:300]}
            logger.warning(
                "Логин BTech %s: %s",
                last_resp.status_code,
                err.get("errors") or err,
            )
            errors = (err.get("errors") or {}) if isinstance(err, dict) else {}
            # Too many login attempt — соседние коды только усугубляют блокировку.
            if "password" in errors and any("many" in str(x).lower() for x in errors["password"]):
                rate_limited = True
                break
            if code is None:
                break

        if rate_limited:
            self._cooldown_until = time.time() + _LOGIN_COOLDOWN_SEC
            raise RuntimeError(
                f"BTech login rate-limited — пауза {_LOGIN_COOLDOWN_SEC // 60} мин"
            )

        if last_resp is None or not last_resp.ok:
            detail = ""
            try:
                detail = (last_resp.text or "")[:400] if last_resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(
                f"BTech login failed ({getattr(last_resp, 'status_code', '?')}): {detail}"
            )
        if data is None:
            data = last_resp.json()

        token = self._extract_token(data)
        if not token:
            token = _find_jwt_recursive(data)
            if token:
                logger.warning(
                    "Токен не нашёлся по ключу '%s', но найден автоматически "
                    "(похожая на JWT строка) в другом месте тела ответа.",
                    settings.LOGIN_TOKEN_RESPONSE_FIELD,
                )

        if not token:
            token = _find_jwt_in_headers(last_resp.headers)

        if not token:
            raise RuntimeError(
                f"Не нашли токен ни в теле ответа (ключ '{settings.LOGIN_TOKEN_RESPONSE_FIELD}' "
                f"и автопоиск JWT), ни в заголовках ответа. "
                f"Похоже, токен приходит через cookie с httpOnly."
            )

        self._token = token
        self._exp = self._decode_exp(token)
        self._cooldown_until = 0.0
        self._persist_locked()
        logger.info("Токен обновлён, истекает: %s", time.ctime(self._exp))

    def _extract_token(self, data):
        # Поддержка как плоского ответа {"token": "..."}, так и вложенного
        # {"data": {"token": "..."}} — распространённый вариант API.
        if isinstance(data, dict):
            if settings.LOGIN_TOKEN_RESPONSE_FIELD in data:
                return data[settings.LOGIN_TOKEN_RESPONSE_FIELD]
            nested = data.get("data")
            if isinstance(nested, dict) and settings.LOGIN_TOKEN_RESPONSE_FIELD in nested:
                return nested[settings.LOGIN_TOKEN_RESPONSE_FIELD]
        return None

    @staticmethod
    def _decode_exp(token: str) -> float:
        """Достаём 'exp' прямо из тела JWT, не проверяя подпись — нам нужно
        только знать, когда токен протухнет, чтобы обновить его заранее."""
        try:
            payload_b64 = token.split(".")[1]
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            return float(payload.get("exp", time.time() + 3600))
        except Exception:
            logger.warning("Не удалось распарсить exp из токена, ставим запас 1 час.")
            return time.time() + 3600


token_manager = TokenManager()
