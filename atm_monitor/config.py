import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]  # корень проекта integratsya
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


class Settings:
    # --- Platform base ---
    API_BASE_URL = os.getenv("API_BASE_URL", "https://monitoring.btech.uz")
    ATM_LIST_PATH = os.getenv("ATM_LIST_PATH", "/api/base/atm/")

    # --- Login / auth ---
    # Подтверждено по вашему логин-запросу в браузере.
    LOGIN_PATH = os.getenv("LOGIN_PATH", "/api/login")
    AUTH_USERNAME = os.getenv("AUTH_USERNAME", "")
    AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")

    # Названия полей в теле запроса логина и в ответе — подтверждены по вашему запросу.
    LOGIN_USERNAME_FIELD = os.getenv("LOGIN_USERNAME_FIELD", "email")
    LOGIN_PASSWORD_FIELD = os.getenv("LOGIN_PASSWORD_FIELD", "password")
    LOGIN_OTP_FIELD = os.getenv("LOGIN_OTP_FIELD", "otpCode")
    LOGIN_TOKEN_RESPONSE_FIELD = os.getenv("LOGIN_TOKEN_RESPONSE_FIELD", "token")
    # TOTP secret из otpauth://…?secret=… (Google Authenticator). Если задан —
    # сборщик сам генерирует otpCode при каждом логине.
    AUTH_OTP_SECRET = os.getenv("AUTH_OTP_SECRET", "").replace(" ", "").strip()

    # --- Database ---
    # Та же база PostgreSQL, что и у платформы; таблицы — в схеме atm_monitor.
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/bank_intelligence")

    # --- Scheduling ---
    # Опрос раз в 15 минут: точнее видны инкассации, часы без денег и время снятий.
    # POLL_INTERVAL_HOURS (старое название) учитывается, если POLL_INTERVAL_MINUTES не задан.
    POLL_INTERVAL_MINUTES = int(
        os.getenv("POLL_INTERVAL_MINUTES")
        or (int(os.getenv("POLL_INTERVAL_HOURS")) * 60 if os.getenv("POLL_INTERVAL_HOURS") else 15)
    )
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
    PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "500"))
    RUN_IMMEDIATELY_ON_START = _bool("RUN_IMMEDIATELY_ON_START", "true")
    # Вместе с сервером запускать фоновый сборщик (ATM_COLLECTOR=0 — выключить)
    COLLECTOR_ENABLED = _bool("ATM_COLLECTOR", "true")
    TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent")

    # --- Прогноз снятий (XGBoost, atm_monitor/forecast.py) ---
    # Модель переобучается раз в сутки в FORECAST_HOUR:00 по TIMEZONE.
    FORECAST_ENABLED = _bool("ATM_FORECAST", "true")
    FORECAST_HOUR = int(os.getenv("FORECAST_HOUR", "0"))
    FORECAST_DAYS = int(os.getenv("FORECAST_DAYS", "7"))
    FORECAST_HISTORY_DAYS = int(os.getenv("FORECAST_HISTORY_DAYS", "120"))
    # Те же пороги, что у карты (api/core/config.py читает те же переменные).
    LOW_CASH_PCT = float(os.getenv("LOW_CASH_PCT", "0.20"))
    DEFAULT_CAPACITY = int(os.getenv("DEFAULT_CAPACITY", "400000000"))

    # --- Инкассации из карточки банкомата (atm_monitor/encashments.py) ---
    # Путь с {atm_id}. Пустое значение выключает сбор инкассаций.
    ATM_ENCASHMENT_PATH = os.getenv("ATM_ENCASHMENT_PATH", "/api/base/atm/{atm_id}/encashment")
    ATM_REPORT_PATH = os.getenv("ATM_REPORT_PATH", "/api/report/atm/{atm_id}")
    # Обновление истории по дням: последние дни могут уточняться задним числом.
    DAILY_HISTORY_REFRESH_DAYS = int(os.getenv("DAILY_HISTORY_REFRESH_DAYS", "7"))
    ENCASHMENT_HISTORY_DAYS = int(os.getenv("ENCASHMENT_HISTORY_DAYS", "92"))
    ENCASHMENT_SYNC_HOURS = int(os.getenv("ENCASHMENT_SYNC_HOURS", "6"))
    # Запросов столько же, сколько банкоматов, поэтому между ними пауза.
    ENCASHMENT_REQUEST_PAUSE_SEC = float(os.getenv("ENCASHMENT_REQUEST_PAUSE_SEC", "0.3"))

    # --- Query params (как в примере пользователя; agentConnectionStatus="all",
    # чтобы получать в т.ч. офлайн-банкоматы, а не только online) ---
    QUERY_PARAMS = {
        "clientId": os.getenv("FILTER_CLIENT_ID", ""),
        "vendorId": os.getenv("FILTER_VENDOR_ID", ""),
        "modelId": os.getenv("FILTER_MODEL_ID", ""),
        "functionId": os.getenv("FILTER_FUNCTION_ID", ""),
        "variantId": os.getenv("FILTER_VARIANT_ID", ""),
        "atmGroupId": os.getenv("FILTER_ATM_GROUP_ID", ""),
        "countryId": os.getenv("FILTER_COUNTRY_ID", ""),
        "regionId": os.getenv("FILTER_REGION_ID", ""),
        "cityId": os.getenv("FILTER_CITY_ID", ""),
        "hashTags": os.getenv("FILTER_HASH_TAGS", ""),
        "appConnectionStatus": os.getenv("FILTER_APP_CONN_STATUS", "all"),
        "agentConnectionStatus": os.getenv("FILTER_AGENT_CONN_STATUS", "all"),
        "hwFaults": os.getenv("FILTER_HW_FAULTS", ""),
        "atmStatus": os.getenv("FILTER_ATM_STATUS", "all"),
        "withUnitsTurnoverTotal": "true",
        "lang": os.getenv("FILTER_LANG", "ru"),
    }


settings = Settings()
