"""
Авторизация, роли и постраничные права доступа.

Хранилище — таблицы roles / users в общей базе PostgreSQL. Пароли хэшируются pbkdf2_hmac.

Права — на «page-key» (видимость модуля/слоя). Импорт и служебные экраны —
только у is_admin. PAGE_DEFINITIONS — единый источник для админки и auth_gate.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from typing import Any, Dict, List, Optional, Set

from .db import connect

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# PAGE-KEYS — видимость модулей (без импорта)
# ═══════════════════════════════════════════════════════════

# parent — вложенность в админке (карточка → кнопки/вкладки внутри).
# home_card — карточка на главной; feature — кнопка/вкладка внутри страницы.
PAGE_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "key": "atm_map",
        "label": "Карта банкоматов",
        "hint": "Карточка на главной и страница карты (ATM)",
        "group": "atm",
        "group_label": "Модуль 1 — Банкоматы и инкассация",
        "parent": None,
        "kind": "home_card",
        "html": ["/dashboard/map.html", "/dashboard/atm-card.html"],
        "api_prefixes": ["/api/atms", "/api/alerts", "/api/baseline", "/api/atm-monitor"],
    },
    {
        "key": "map_branches",
        "label": "На карте: филиалы",
        "hint": "KPI «Филиалы», меню филиалов/склад на карте",
        "group": "atm",
        "group_label": "Модуль 1 — Банкоматы и инкассация",
        "parent": "atm_map",
        "kind": "feature",
        "html": [],
        "api_prefixes": ["/api/branches"],
    },
    {
        "key": "map_vash",
        "label": "На карте и в аналитике: ВАШ",
        "hint": "Слой ВАШ на карте + вкладка «ВАШ» в аналитике",
        "group": "atm",
        "group_label": "Модуль 1 — Банкоматы и инкассация",
        "parent": "atm_map",
        "kind": "feature",
        "html": [],
        "api_prefixes": ["/api/vash"],
    },
    {
        "key": "atm_analytics",
        "label": "Аналитика",
        "hint": "Карточка и страница: состояние сети, прогноз, снятия, 400 млн, инкассации, ABC",
        "group": "atm",
        "group_label": "Модуль 1 — Банкоматы и инкассация",
        "parent": None,
        "kind": "home_card",
        "html": ["/dashboard/analytics.html"],
        "api_prefixes": ["/api/analytics", "/api/transactions"],
    },
    {
        "key": "treasury",
        "label": "Касса филиалов",
        "hint": "Карточка на главной + вкладка «Касса» внутри аналитики",
        "group": "atm",
        "group_label": "Модуль 1 — Банкоматы и инкассация",
        "parent": None,
        "kind": "home_card",
        # analytics.html — чтобы открыть вкладку «Касса» без полного ATM-доступа
        "html": ["/dashboard/branch-cash.html", "/dashboard/analytics.html"],
        "api_prefixes": ["/api/branch-balances", "/api/branch-ledger", "/api/sqb-rates"],
    },
    {
        "key": "atm_plan",
        "label": "План инкассаций",
        "hint": "Карточка и страница плана",
        "group": "atm",
        "group_label": "Модуль 1 — Банкоматы и инкассация",
        "parent": None,
        "kind": "home_card",
        "html": ["/dashboard/incassation-plan.html", "/dashboard/equipment-replacement.html"],
        "api_prefixes": [
            "/api/incassation", "/api/routes", "/api/osrm", "/api/cash-equipment", "/api/branches",
        ],
    },
    {
        "key": "atm_savings",
        "label": "Экономия",
        "hint": "Карточка и страница экономии инкассации",
        "group": "atm",
        "group_label": "Модуль 1 — Банкоматы и инкассация",
        "parent": None,
        "kind": "home_card",
        "html": ["/dashboard/savings.html"],
        "api_prefixes": ["/api/savings", "/api/settings"],
    },
    {
        "key": "atm_calendar",
        "label": "Календарь инкассации",
        "hint": "Карточка и календарь выездов",
        "group": "atm",
        "group_label": "Модуль 1 — Банкоматы и инкассация",
        "parent": None,
        "kind": "home_card",
        "html": ["/dashboard/incassation.html"],
        "api_prefixes": ["/api/incassation", "/api/routes", "/api/osrm"],
    },
    {
        "key": "hr_cashiers",
        "label": "Аналитика кассиров",
        "hint": "Карточка и раздел кассиров (без импорта)",
        "group": "cashiers",
        "group_label": "Модуль 2 — Кассиры",
        "parent": None,
        "kind": "home_card",
        "html": ["/dashboard/cashiers.html", "/dashboard/cashier-detail.html"],
        "api_prefixes": ["/api/cashiers"],
    },
]

PAGE_KEYS = [p["key"] for p in PAGE_DEFINITIONS]
PAGE_BY_KEY = {p["key"]: p for p in PAGE_DEFINITIONS}

# Хабы без чувствительных данных — любому вошедшему.
FREE_HTML_PATHS = {
    "/",
    "/dashboard/index.html",
    "/tashkent_districts.geojson",
    "/uzbekistan.geojson",
    "/uzbekistan_regional.geojson",
}

# Только администратор (импорт / служебное).
ADMIN_ONLY_PREFIXES = (
    "/admin",
    "/api/data-console",
    "/dashboard/import",
    "/dashboard/data-console.html",
    "/dashboard/atm-monitor.html",
    "/dashboard/import-vash.html",
    "/dashboard/import-atms.html",
    "/dashboard/import-branches.html",
    "/dashboard/import-cash-equipment.html",
    "/dashboard/import-branch-balances.html",
    "/dashboard/import-sqb-rates.html",
    "/dashboard/import-cashiers.html",
)

PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/change-password",
    "/api/health",
    "/dashboard/login.html",
}

# API, доступные при must_change_password (пока не сменили пароль).
PASSWORD_CHANGE_ALLOWED = {
    "/api/auth/me",
    "/api/auth/logout",
    "/api/auth/change-password",
    "/dashboard/login.html",
}


def resolve_page_keys(path: str) -> List[str]:
    """Путь → все page-key, которые его покрывают (достаточно одного у пользователя)."""
    keys: List[str] = []
    for pdef in PAGE_DEFINITIONS:
        if path in pdef["html"]:
            keys.append(pdef["key"])
            continue
        for prefix in pdef["api_prefixes"]:
            if (
                path == prefix
                or path.startswith(prefix + "/")
                or path.startswith(prefix + "?")
                or path.rstrip("/") == prefix
            ):
                keys.append(pdef["key"])
                break
    return keys


def resolve_page_key(path: str) -> Optional[str]:
    """Обратная совместимость: первый совпавший ключ."""
    keys = resolve_page_keys(path)
    return keys[0] if keys else None


def is_admin_only_path(path: str) -> bool:
    if path.startswith("/admin"):
        return True
    for pref in ADMIN_ONLY_PREFIXES:
        if path == pref or path.startswith(pref):
            return True
    return False


def user_can_access(user: Dict[str, Any], path: str) -> bool:
    if user.get("is_admin"):
        return True
    if path in FREE_HTML_PATHS:
        return True
    if is_admin_only_path(path):
        return False
    keys = resolve_page_keys(path)
    if not keys:
        return True  # статика / неразмеченное — данные закрыты на /api/*
    allowed = set(user.get("pages") or [])
    return bool(allowed.intersection(keys))


def pages_for_admin_ui() -> List[Dict[str, Any]]:
    return [
        {
            "key": p["key"],
            "label": p["label"],
            "hint": p.get("hint") or "",
            "group": p["group"],
            "group_label": p["group_label"],
            "parent": p.get("parent"),
            "kind": p.get("kind") or "home_card",
        }
        for p in PAGE_DEFINITIONS
    ]


# ═══════════════════════════════════════════════════════════
# ПАРОЛИ
# ═══════════════════════════════════════════════════════════

_PBKDF2_ITERATIONS = 400_000
# Dummy salt/hash so missing users still spend verify time (timing hardening).
_DUMMY_SALT = "00" * 16
_DUMMY_HASH = hashlib.pbkdf2_hmac(
    "sha256", b"not-a-real-password", bytes.fromhex(_DUMMY_SALT), _PBKDF2_ITERATIONS
).hex()


def _hash_password(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return {"salt": salt, "hash": digest.hex()}


def _verify_password(password: str, salt: str, expected_hash: str) -> bool:
    candidate = _hash_password(password, salt)["hash"]
    return secrets.compare_digest(candidate, expected_hash)


def generate_password(length: int = 14) -> str:
    """Always includes letter + digit so it passes validate_password_strength."""
    from .security import MIN_PASSWORD_LENGTH

    length = max(length, MIN_PASSWORD_LENGTH)
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    digits = "23456789"
    alphabet = letters + digits
    chars = [secrets.choice(letters), secrets.choice(digits)]
    chars += [secrets.choice(alphabet) for _ in range(length - 2)]
    # Shuffle so letter/digit positions are not fixed.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


# ═══════════════════════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ + СИДИРОВАНИЕ
# ═══════════════════════════════════════════════════════════

_DEFAULT_ROLES = [
    # (name, label, is_admin, pages) — ATM без филиалов/ВАШ на карте и без кассы
    ("admin", "Администратор", 1, PAGE_KEYS),
    (
        "atm_manager",
        "Банкоматы",
        0,
        ["atm_map", "atm_analytics", "atm_savings", "atm_plan", "atm_calendar"],
    ),
    ("treasury", "Касса", 0, ["treasury"]),
    ("hr_analyst", "Кассиры", 0, ["hr_cashiers"]),
    ("viewer", "Наблюдатель", 0, []),
]


def _normalize_pages(pages: Any) -> List[str]:
    """Оставляем только известные ключи. Ничего не дописываем автоматически —
    каждый пункт видимости задаётся явно в админке."""
    raw: Set[str] = set()
    if isinstance(pages, str):
        try:
            pages = json.loads(pages)
        except Exception:
            pages = []
    for p in pages or []:
        if isinstance(p, str):
            raw.add(p)
    return [k for k in PAGE_KEYS if k in raw]


def create_auth_tables(conn) -> Optional[str]:
    """Создаёт таблицы и (при первом запуске) сеет роли + пользователя admin."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            name       TEXT NOT NULL UNIQUE,
            label      TEXT NOT NULL,
            is_admin   SMALLINT NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
            pages_json JSONB NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            full_name     TEXT,
            role_id       BIGINT NOT NULL REFERENCES roles(id),
            is_active     SMALLINT NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            must_change_password SMALLINT NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # Миграция для уже существующих БД
    conn.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "must_change_password SMALLINT NOT NULL DEFAULT 0"
    )
    # Персональная видимость модулей (если NULL — берём из роли)
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pages_json JSONB")

    n_roles = int(conn.execute("SELECT COUNT(*) AS c FROM roles").fetchone()["c"])
    if n_roles == 0:
        for name, label, is_admin, pages in _DEFAULT_ROLES:
            conn.execute(
                "INSERT INTO roles (name, label, is_admin, pages_json) VALUES (%s,%s,%s,%s)",
                (name, label, is_admin, json.dumps(pages)),
            )
        log.info("Роли по умолчанию созданы: %s", ", ".join(r[0] for r in _DEFAULT_ROLES))
    else:
        # Мягкая миграция старых ролей: atm_map раньше включал план/календарь,
        # atm_analytics — экономию. Филиалы/ВАШ сами не добавляем.
        label_by_name = {name: label for name, label, _, _ in _DEFAULT_ROLES}
        for row in conn.execute("SELECT id, name, is_admin, pages_json FROM roles").fetchall():
            if row["is_admin"]:
                conn.execute(
                    "UPDATE roles SET pages_json = %s, label = COALESCE(%s, label) WHERE id = %s",
                    (json.dumps(PAGE_KEYS), label_by_name.get(row["name"]), row["id"]),
                )
            else:
                raw = set(_normalize_pages(row["pages_json"]))
                if "atm_map" in raw:
                    raw.update({"atm_plan", "atm_calendar"})
                if "atm_analytics" in raw:
                    raw.add("atm_savings")
                new_label = label_by_name.get(row["name"])
                conn.execute(
                    "UPDATE roles SET pages_json = %s, label = COALESCE(%s, label) WHERE id = %s",
                    (json.dumps([k for k in PAGE_KEYS if k in raw]), new_label, row["id"]),
                )

    n_users = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
    if n_users:
        return None

    from .config import ADMIN_PASSWORD, ADMIN_USERNAME
    from .security import validate_password_strength

    admin_role = conn.execute("SELECT id FROM roles WHERE name = 'admin'").fetchone()
    username = (ADMIN_USERNAME or "admin").strip() or "admin"
    if ADMIN_PASSWORD:
        err = validate_password_strength(ADMIN_PASSWORD)
        if err:
            raise ValueError(f"ADMIN_PASSWORD в .env слабый: {err}")
        seeded_password = ADMIN_PASSWORD
        must_change = 0
    else:
        seeded_password = generate_password()
        must_change = 1
    creds = _hash_password(seeded_password)
    conn.execute(
        """INSERT INTO users (username, password_hash, password_salt, full_name, role_id,
                              is_active, must_change_password)
           VALUES (%s, %s, %s, 'Администратор', %s, 1, %s)""",
        (username, creds["hash"], creds["salt"], admin_role["id"], must_change),
    )
    return seeded_password


def sync_admin_password_from_env() -> bool:
    """Если ADMIN_PASSWORD_SYNC=1 и ADMIN_PASSWORD задан — обновить пароль admin из .env.

    Нужно для банк-сервера: пароль кладут в серверный .env (не в git), после
    рестарта вход совпадает. В git секреты не коммитим.
    """
    from .config import ADMIN_PASSWORD, ADMIN_PASSWORD_SYNC, ADMIN_USERNAME
    from .security import validate_password_strength

    if not ADMIN_PASSWORD_SYNC or not ADMIN_PASSWORD:
        return False
    err = validate_password_strength(ADMIN_PASSWORD)
    if err:
        log.error("ADMIN_PASSWORD_SYNC пропущен: %s", err)
        return False
    username = (ADMIN_USERNAME or "admin").strip() or "admin"
    creds = _hash_password(ADMIN_PASSWORD)
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = %s", (username,)
        ).fetchone()
        if not row:
            admin_role = conn.execute("SELECT id FROM roles WHERE name = 'admin'").fetchone()
            if not admin_role:
                log.error("ADMIN_PASSWORD_SYNC: роль admin не найдена")
                return False
            conn.execute(
                """INSERT INTO users (username, password_hash, password_salt, full_name, role_id,
                                      is_active, must_change_password)
                   VALUES (%s,%s,%s,'Администратор',%s,1,0)""",
                (username, creds["hash"], creds["salt"], admin_role["id"]),
            )
        else:
            conn.execute(
                """UPDATE users SET password_hash = %s, password_salt = %s,
                   must_change_password = 0, is_active = 1 WHERE id = %s""",
                (creds["hash"], creds["salt"], row["id"]),
            )
    write_admin_credentials(ADMIN_PASSWORD)
    log.warning(
        "Пароль пользователя %r синхронизирован из .env (ADMIN_PASSWORD_SYNC=1).",
        username,
    )
    return True


def write_admin_credentials(password: str) -> None:
    from .config import ADMIN_USERNAME, DATA_DIR

    creds_path = DATA_DIR / "admin_credentials.txt"
    creds_path.write_text(
        f"Логин: {ADMIN_USERNAME or 'admin'}\nПароль: {password}\n\n"
        "Хранится только на сервере (data/ в .gitignore). Не коммитьте в git.\n",
        encoding="utf-8",
    )
    log.warning(
        "Пароль пользователя admin сохранён в %s — смените после первого входа (пароль в лог не пишем).",
        creds_path,
    )


# ═══════════════════════════════════════════════════════════
# ПОЛЬЗОВАТЕЛИ
# ═══════════════════════════════════════════════════════════

def _row_to_user(row: Dict[str, Any]) -> Dict[str, Any]:
    # Персональные pages пользователя важнее роли (кроме is_admin).
    user_pages = row.get("user_pages_json")
    role_pages = row.get("pages_json") or []
    if row.get("is_admin"):
        pages = list(PAGE_KEYS)
    elif user_pages is not None:
        pages = _normalize_pages(user_pages)
    else:
        pages = _normalize_pages(role_pages)
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row.get("full_name"),
        "is_active": bool(row["is_active"]),
        "must_change_password": bool(row.get("must_change_password")),
        "role_id": row["role_id"],
        "role_name": row.get("role_name"),
        "role_label": row.get("role_label"),
        "is_admin": bool(row.get("is_admin")),
        "pages": pages,
    }


_USER_SELECT = """
    SELECT u.id, u.username, u.full_name, u.is_active, u.role_id,
           u.must_change_password, u.pages_json AS user_pages_json,
           r.name AS role_name, r.label AS role_label, r.is_admin, r.pages_json
    FROM users u JOIN roles r ON r.id = u.role_id
"""


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(f"{_USER_SELECT} WHERE u.id = %s", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, password_hash, password_salt, is_active FROM users WHERE username = %s",
            (username.strip(),),
        ).fetchone()
    if not row or not row["is_active"]:
        # Constant-ish work so attackers cannot probe usernames by timing.
        _verify_password(password or "x", _DUMMY_SALT, _DUMMY_HASH)
        return None
    if not _verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return get_user_by_id(row["id"])


def list_users() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(f"{_USER_SELECT} ORDER BY u.username").fetchall()
    return [_row_to_user(r) for r in rows]


def _resolve_role_id(conn, *, make_admin: bool, role_id: Optional[int] = None) -> int:
    if make_admin:
        row = conn.execute("SELECT id FROM roles WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
        if not row:
            raise ValueError("Роль администратора не найдена")
        return int(row["id"])
    if role_id is not None:
        row = conn.execute(
            "SELECT id FROM roles WHERE id = %s AND is_admin = 0", (role_id,)
        ).fetchone()
        if row:
            return int(row["id"])
    row = conn.execute(
        "SELECT id FROM roles WHERE name = 'viewer' OR is_admin = 0 ORDER BY is_admin, id LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("Нет роли для обычного пользователя")
    return int(row["id"])


def create_user(
    username: str,
    password: Optional[str] = None,
    full_name: Optional[str] = None,
    role_id: Optional[int] = None,
    pages: Optional[List[str]] = None,
    *,
    is_admin: bool = False,
    must_change: bool = True,
) -> Dict[str, Any]:
    """Создаёт пользователя. pages — что видит (вместе с логином).
    Если password пуст — генерируется одноразовый."""
    username = username.strip()
    if not username:
        raise ValueError("Логин не может быть пустым")
    from .security import validate_password_strength

    generated = not bool(password)
    temp = password or generate_password()
    err = validate_password_strength(temp)
    if err:
        raise ValueError(err)
    creds = _hash_password(temp)
    page_list = list(PAGE_KEYS) if is_admin else _normalize_pages(pages or [])
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone()
        if exists:
            raise ValueError(f"Логин {username!r} уже занят")
        rid = _resolve_role_id(conn, make_admin=is_admin, role_id=role_id)
        new_id = conn.execute(
            """INSERT INTO users (username, password_hash, password_salt, full_name, role_id,
                                  is_active, must_change_password, pages_json)
               VALUES (%s,%s,%s,%s,%s,1,%s,%s) RETURNING id""",
            (
                username,
                creds["hash"],
                creds["salt"],
                full_name,
                rid,
                int(must_change),
                None if is_admin else json.dumps(page_list),
            ),
        ).fetchone()["id"]
    user = get_user_by_id(new_id)
    assert user is not None
    if must_change or generated:
        user["temporary_password"] = temp
    return user


def update_user(
    user_id: int,
    full_name: Optional[str] = None,
    role_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    new_password: Optional[str] = None,
    pages: Optional[List[str]] = None,
    *,
    is_admin: Optional[bool] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        if not row:
            raise ValueError("Пользователь не найден")
        if full_name is not None:
            conn.execute("UPDATE users SET full_name = %s WHERE id = %s", (full_name, user_id))
        if is_admin is not None:
            rid = _resolve_role_id(conn, make_admin=is_admin, role_id=role_id)
            conn.execute("UPDATE users SET role_id = %s WHERE id = %s", (rid, user_id))
            if is_admin:
                conn.execute("UPDATE users SET pages_json = NULL WHERE id = %s", (user_id,))
        elif role_id is not None:
            role = conn.execute("SELECT id FROM roles WHERE id = %s", (role_id,)).fetchone()
            if not role:
                raise ValueError("Роль не найдена")
            conn.execute("UPDATE users SET role_id = %s WHERE id = %s", (role_id, user_id))
        if pages is not None and not (is_admin is True):
            # Не затираем admin
            cur = conn.execute(
                """SELECT r.is_admin FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = %s""",
                (user_id,),
            ).fetchone()
            if cur and not cur["is_admin"]:
                conn.execute(
                    "UPDATE users SET pages_json = %s WHERE id = %s",
                    (json.dumps(_normalize_pages(pages)), user_id),
                )
        if is_active is not None:
            conn.execute(
                "UPDATE users SET is_active = %s WHERE id = %s", (int(is_active), user_id)
            )
        if new_password:
            from .security import validate_password_strength

            _pw_err = validate_password_strength(new_password)
            if _pw_err:
                raise ValueError(_pw_err)
            creds = _hash_password(new_password)
            conn.execute(
                """UPDATE users SET password_hash = %s, password_salt = %s,
                   must_change_password = 0 WHERE id = %s""",
                (creds["hash"], creds["salt"], user_id),
            )
    return get_user_by_id(user_id)


def reset_user_password(user_id: int) -> Dict[str, Any]:
    """Новый одноразовый пароль + must_change_password=1."""
    temp = generate_password()
    creds = _hash_password(temp)
    with connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
        if not row:
            raise ValueError("Пользователь не найден")
        conn.execute(
            """UPDATE users SET password_hash = %s, password_salt = %s,
               must_change_password = 1 WHERE id = %s""",
            (creds["hash"], creds["salt"], user_id),
        )
    user = get_user_by_id(user_id)
    assert user is not None
    user["temporary_password"] = temp
    return user


def change_password(user_id: int, current_password: str, new_password: str) -> Dict[str, Any]:
    from .security import validate_password_strength

    err = validate_password_strength(new_password or "")
    if err:
        raise ValueError(err)
    if current_password == new_password:
        raise ValueError("Новый пароль должен отличаться от текущего")
    with connect() as conn:
        row = conn.execute(
            "SELECT password_hash, password_salt FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
        if not row:
            raise ValueError("Пользователь не найден")
        if not _verify_password(current_password, row["password_salt"], row["password_hash"]):
            raise ValueError("Неверный текущий пароль")
        creds = _hash_password(new_password)
        conn.execute(
            """UPDATE users SET password_hash = %s, password_salt = %s,
               must_change_password = 0 WHERE id = %s""",
            (creds["hash"], creds["salt"], user_id),
        )
    return get_user_by_id(user_id)


def delete_user(user_id: int) -> None:
    with connect() as conn:
        remaining_admins = conn.execute(
            """SELECT COUNT(*) AS c FROM users u JOIN roles r ON r.id = u.role_id
               WHERE r.is_admin = 1 AND u.id != %s AND u.is_active = 1""",
            (user_id,),
        ).fetchone()["c"]
        target = conn.execute(
            """SELECT r.is_admin FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = %s""",
            (user_id,),
        ).fetchone()
        if target and target["is_admin"] and remaining_admins == 0:
            raise ValueError("Нельзя удалить последнего администратора")
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))


# ═══════════════════════════════════════════════════════════
# РОЛИ
# ═══════════════════════════════════════════════════════════

def _row_to_role(row: Dict[str, Any]) -> Dict[str, Any]:
    pages = row.get("pages_json") or []
    if row.get("is_admin"):
        pages = list(PAGE_KEYS)
    else:
        pages = _normalize_pages(pages)
    return {
        "id": row["id"],
        "name": row["name"],
        "label": row["label"],
        "is_admin": bool(row["is_admin"]),
        "pages": pages,
    }


def list_roles() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM roles ORDER BY id").fetchall()
    return [_row_to_role(r) for r in rows]


def create_role(name: str, label: str, pages: List[str]) -> Dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("Имя роли не может быть пустым")
    pages = [p for p in pages if p in PAGE_KEYS]
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM roles WHERE name = %s", (name,)).fetchone()
        if exists:
            raise ValueError(f"Роль {name!r} уже существует")
        new_id = conn.execute(
            "INSERT INTO roles (name, label, is_admin, pages_json) VALUES (%s,%s,0,%s) RETURNING id",
            (name, label or name, json.dumps(pages)),
        ).fetchone()["id"]
    return _row_to_role(_fetch_role(new_id))


def _fetch_role(role_id: int) -> Dict[str, Any]:
    with connect() as conn:
        return conn.execute("SELECT * FROM roles WHERE id = %s", (role_id,)).fetchone()


def update_role(
    role_id: int, label: Optional[str] = None, pages: Optional[List[str]] = None
) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM roles WHERE id = %s", (role_id,)).fetchone()
        if not row:
            raise ValueError("Роль не найдена")
        if row["is_admin"] and pages is not None:
            raise ValueError(
                "У роли администратора список страниц не редактируется — у неё всегда полный доступ"
            )
        if label is not None:
            conn.execute("UPDATE roles SET label = %s WHERE id = %s", (label, role_id))
        if pages is not None:
            pages = [p for p in pages if p in PAGE_KEYS]
            conn.execute(
                "UPDATE roles SET pages_json = %s WHERE id = %s",
                (json.dumps(pages), role_id),
            )
    return _row_to_role(_fetch_role(role_id))


def delete_role(role_id: int) -> None:
    with connect() as conn:
        row = conn.execute("SELECT is_admin FROM roles WHERE id = %s", (role_id,)).fetchone()
        if not row:
            raise ValueError("Роль не найдена")
        if row["is_admin"]:
            raise ValueError("Роль администратора нельзя удалить")
        in_use = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role_id = %s", (role_id,)
        ).fetchone()["c"]
        if in_use:
            raise ValueError(
                f"На эту роль назначено пользователей: {in_use} — сначала переназначьте их"
            )
        conn.execute("DELETE FROM roles WHERE id = %s", (role_id,))
