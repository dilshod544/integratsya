"""
Единая конфигурация Bank Intelligence Platform.

Объединяет настройки двух доменов:
  * Cashier Intelligence — аналитика кассиров
  * ATM Monitor         — реестр банкоматов, остатки, инкассация

Хранилище — PostgreSQL (строка подключения в DATABASE_URL).
"""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

# ── Пути ────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parents[2]   # корень проекта
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
load_dotenv(BASE_DIR / ".env")

# ── База данных (PostgreSQL) ─────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/bank_intelligence")
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "30"))

# ── Бизнес-пороги ATM ────────────────────────────────────────
LOW_CASH_PCT      = float(os.getenv("LOW_CASH_PCT", "0.20"))      # критичный уровень (cash-out)
WARNING_CASH_PCT  = float(os.getenv("WARNING_CASH_PCT", "0.40"))  # уровень предупреждения
DEFAULT_CAPACITY  = int(os.getenv("DEFAULT_CAPACITY", "400000000"))  # ёмкость ATM по умолчанию, UZS

# ── Геометрия / маршруты инкассации ─────────────────────────
DEPOT = {"lat": 41.3111, "lon": 69.2797, "name": "Центральный депо (Ташкент)"}
ROAD_FACTOR = 1.35      # коэффициент дорожной сети к прямому расстоянию
AVG_SPEED_KMH = 30      # средняя скорость инкассаторской машины

# ── Сессии / авторизация ─────────────────────────────────────
# AUTH_DISABLED=1 — открыть все страницы и API без входа (только локальное тестирование).
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "0").strip().lower() in ("1", "true", "yes")
# Подписывает cookie сессии (see api/main.py SessionMiddleware). Без своего
# SECRET_KEY в .env генерится случайный при каждом запуске — тогда после
# каждого перезапуска сервера все сессии слетают (заново логиниться).
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("SECRET_KEY"):
    import logging
    logging.getLogger(__name__).warning(
        "SECRET_KEY не задан в .env — сгенерирован временный, сессии слетят при перезапуске сервера. "
        "Для продакшена задайте свой SECRET_KEY в .env."
    )
