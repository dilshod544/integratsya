"""
Bank Intelligence Platform — FastAPI Backend
=============================================

Объединяет два продукта в одном приложении и одной базе PostgreSQL:

  ATM Monitor (реестр банкоматов, остатки, инкассация)
    GET   /api/atms                         → список ATM из БД
    GET   /api/atms/stats                   → статистика по областям/филиалам
    GET   /api/atms/{terminal_id}           → детали ATM
    GET   /api/atms/{terminal_id}/cassettes → кассеты + baseline
    POST  /api/atms/import                  → загрузка XLSX реестра ATM
    POST  /api/atms/import/clear            → очистить реестр ATM
    POST  /api/atms/{terminal_id}/balance   → обновить остаток
    POST  /api/atms/balances/bulk           → массовое обновление остатков
    GET   /api/branches                     → список филиалов
    GET   /api/branches/stats               → статистика по филиалам
    GET   /api/branches/{local_code}        → детали филиала
    POST  /api/branches/import              → загрузка XLSX реестра филиалов
    POST  /api/branches/import/clear        → очистить реестр филиалов
    POST  /api/branches/balances/import     → импорт кассовых остатков (отдельный XLSX)
    POST  /api/branches/balances/clear      → очистить кассовые остатки
    GET   /api/branch-balances/analytics    → сводка кассы по регионам
    POST  /api/cash-equipment/import        → импорт списка кассовой техники (XLSX)
    POST  /api/cash-equipment/clear         → очистить кассовую технику
    GET   /api/cash-equipment               → список (фильтр ?local_code=)
    GET   /api/cash-equipment/branches      → техника, сгруппированная по филиалам
    GET   /api/cash-equipment/replacement   → план замены (износ, дата списания)
    POST  /api/vash/import                  → импорт списка ВАШ (валюта айирбошлаш шохобчалари)
    POST  /api/vash/clear                   → очистить список ВАШ
    GET   /api/vash/branches                → ВАШ, сгруппированные по филиалам (для карты)
    GET   /api/alerts                       → ATM в critical/warning
    GET   /api/baseline                     → сводный отчёт по остаткам
    POST  /api/routes/incassation           → региональные маршруты инкассации
    POST  /api/osrm/route                   → геометрия маршрута по дорогам (OSRM)
    GET   /api/incassation/plan             → прогнозный план инкассации
    GET   /api/analytics/atm-status         → состояние сети BTech: работают/заняты/офлайн/не заряжены
    GET   /api/analytics/forecast           → прогноз снятий (XGBoost, переобучается раз в сутки)
    GET   /api/analytics/withdrawals        → фактические снятия по дням
    GET   /api/analytics/atm-efficiency     → спрос в час и лишние деньги (запас сверх нормы)
    GET   /api/analytics/balance-history    → история остатка ATM / сети по часам
    GET   /api/analytics/atm-history/{id}   → вся история одного банкомата (окно по клику на мини-график)
    POST  /api/analytics/forecast/run       → переобучить модель сейчас

  Cashier Intelligence (аналитика кассиров)
    POST  /api/cashiers/import              → импорт Excel-отчёта KPI кассиров
    GET   /api/cashiers/analytics           → аналитика и структура операций
    GET   /api/cashiers/{report_id}         → углублённая аналитика кассира

Docs: http://localhost:8080/docs
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Гарантируем, что каталог api/ в PYTHONPATH (модули импортируются как core.*)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта: пакет atm_monitor

import uvicorn
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import atm_monitor.api as atm_monitor_api
import atm_monitor.data_console as data_console
import atm_monitor.db as atm_monitor_db
import atm_monitor.forecast as atm_forecast
import atm_monitor.history as atm_history
import atm_monitor.scheduler as atm_collector
from core import ai_assistant, atm_analytics, atm_live, auth, economics, savings
from core import incassation_plan as inc_plan
from core.config import AUTH_DISABLED, BASE_DIR, SECRET_KEY
from core.db import (
    DatabaseUnavailableError,
    bulk_insert_atms,
    bulk_insert_branches,
    bulk_update_balances,
    close_pool,
    count_atms,
    count_branches,
    get_atm,
    get_branch,
    init_db,
    list_atms,
    list_branches,
    list_branches_full,
    list_incassation_trips,
    list_regions,
    replace_incassation_trips,
    truncate_atms,
    truncate_branches,
    update_balance,
)
from core.importer import parse_branches_xlsx, parse_xlsx
from core.branch_balance import (
    balances_by_local_code_map,
    clear_branch_balances,
    get_all_branch_forecasts,
    get_branch_balance_by_local_code,
    get_branch_balance_history,
    list_branch_balances,
    list_editable_limits,
    migrate_balance_import_to_limits,
    parse_branch_balances_xlsx,
    replace_branch_balances,
    save_cash_limit,
)
from core.dataset_imports import KIND_BALANCES, delete_dataset_import, list_dataset_imports
from core.cash_equipment import (
    cash_equipment_by_branch,
    equipment_replacement_plan,
    clear_cash_equipment,
    list_cash_equipment,
    parse_cash_equipment_xlsx,
    replace_cash_equipment,
)
from core.ledger_vash import (
    apply_ledger_to_branches,
    branch_ledger_analytics,
    clear_branch_ledger,
    clear_vash_operations,
    delete_branch_ledger_import,
    delete_vash_operation_import,
    ledger_import_periods,
    list_branch_ledger_imports,
    list_vash_operation_imports,
    parse_branch_ledger_xlsx,
    parse_vash_operations_xlsx,
    replace_branch_ledger,
    replace_vash_operations,
    vash_import_periods,
    vash_operations_analytics,
    export_vash_operations_xlsx,
    ledger_cash_overview,
)
from core.vash import (
    clear_vash_zones,
    count_vash_zones,
    delete_vash_zone_import,
    known_branch_codes,
    list_vash_zone_imports,
    parse_vash_xlsx,
    replace_vash_zones,
    vash_by_branch,
    vash_counts_by_local_code,
)
from core.sqb_rates import (
    latest_rates,
    parse_sqb_rates_xlsx,
    replace_sqb_rates,
)
from core.incassation_router import build_regional_routes, hours_to_low_cash, osrm_route_geometry
from core.cashier import (
    cashier_analytics,
    cashier_detail,
    delete_cashier_import,
    detect_period,
    list_cashier_imports,
    normalize_period,
    parse_cashiers_xlsx,
    save_cashier_import,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger(__name__)


# ── Загрузка файлов ──────────────────────────────────────────

# Битый/не-Excel файл openpyxl отдаёт как BadZipFile/InvalidFileException/KeyError —
# это ошибка пользователя (400), а не сервера (500).
from zipfile import BadZipFile
from openpyxl.utils.exceptions import InvalidFileException

_PARSE_ERRORS = (ValueError, BadZipFile, InvalidFileException, KeyError, OSError)


def _parse_error_text(e: Exception) -> str:
    if isinstance(e, ValueError) and not isinstance(e, BadZipFile):
        return str(e)
    return "Не удалось прочитать файл: это не Excel (.xlsx) или файл повреждён."


def _safe_upload_name(file: UploadFile) -> str:
    """Имя файла от клиента — только базовое имя без каталогов: «../x.xlsx» не должен
    записаться за пределы временной папки."""
    name = os.path.basename((file.filename or "").replace("\\", "/")).strip()
    if name in ("", ".", ".."):
        name = "upload.xlsx"
    return name


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Инициализация БД Bank Intelligence Platform (PostgreSQL)...")
    try:
        init_db()
        migrated = migrate_balance_import_to_limits()
        if migrated["copied"] or migrated["deleted_balance_rows"]:
            log.info(
                "Лимиты кассы сохранены: %s. Старый импорт остатков удалён: %s строк.",
                migrated["copied"], migrated["deleted_balance_rows"],
            )
    except DatabaseUnavailableError as e:
        # Показываем понятную инструкцию вместо сырого traceback драйвера БД
        # и глушим вывод стектрейса Starlette — он здесь ничего не добавляет.
        print(str(e), file=sys.stderr, flush=True)
        logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
        os._exit(1)
    log.info("БД готова. ATM в базе: %d, филиалов: %d", count_atms(), count_branches())
    # История из уже накопленных снимков (например, база перенесена с другого ПК).
    def _history_and_plan():
        atm_history.safe_process_history()
        inc_plan.safe_refresh_plan()

    threading.Thread(target=_history_and_plan, name="atm-history-backfill", daemon=True).start()
    atm_collector.on_after_poll(inc_plan.safe_refresh_plan)
    atm_collector.start_background()   # atm_monitor: опрос BTech в фоне (раз в 15 мин)
    atm_forecast.start_background()    # прогноз снятий: переобучение раз в сутки
    yield
    atm_collector.stop_background()
    atm_forecast.stop_background()
    close_pool()
    atm_monitor_db.close_pool()
    log.info("Сервер остановлен.")


# ── FastAPI App ──────────────────────────────────────────────

app = FastAPI(
    title="Bank Intelligence Platform API",
    description=(
        "Единая платформа: мониторинг банкоматов и инкассации + "
        "аналитика показателей кассиров"
    ),
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(atm_monitor_api.router)

dashboard_dir = BASE_DIR / "dashboard"
if dashboard_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")

admin_dir = BASE_DIR / "admin"
if admin_dir.exists():
    app.mount("/admin", StaticFiles(directory=str(admin_dir), html=True), name="admin")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard/index.html")


@app.get("/api/health", summary="Проверка живости сервиса")
async def health():
    return {
        "ok": True,
        "atms": count_atms(),
        "branches": count_branches(),
        "modules": ["atm-monitor", "cashier-intelligence"],
    }


@app.get("/api/atm-monitor/card", summary="Страница банкомата: всё по id BTech, TID или terminal_id реестра")
def atm_monitor_card(id: Optional[int] = None, tid: Optional[str] = None, terminal: Optional[str] = None):
    atm_id = id
    registry = None
    if atm_id is None and tid:
        atm_id = next(iter(atm_monitor_api.atm_ids_by_tid(tid)), None)
    if atm_id is None and terminal:
        # Как на карте: реестр сопоставляется с BTech по terminal_id или по координатам.
        registry = get_atm(terminal)
        live_tid = registry.get("live_tid") if registry else None
        ids = atm_monitor_api.atm_ids_by_tid(live_tid, terminal) if live_tid else atm_monitor_api.atm_ids_by_terminal(terminal)
        atm_id = next(iter(ids), None)
    card = atm_monitor_api.atm_card_data(atm_id) if atm_id is not None else None
    if card:
        return card
    ref = id or tid or terminal or "?"
    if registry:
        place = registry.get("address") or registry.get("branch") or ""
        raise HTTPException(404, f"Банкомат {ref} есть в реестре ({place}), но BTech по нему данных не отдаёт.")
    raise HTTPException(404, f"Банкомат {ref} не найден.")


@app.get("/api/atm-monitor/status", summary="Статус сборщика atm_monitor (последний опрос BTech)")
async def atm_monitor_status():
    return atm_live.collector_status()


# ═══════════════════════════════════════════════════════════
# АВТОРИЗАЦИЯ / РОЛИ / ДОСТУПЫ
# ═══════════════════════════════════════════════════════════

def _current_user(request: Request) -> Optional[Dict[str, Any]]:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = auth.get_user_by_id(uid)
    if not user or not user["is_active"]:
        return None
    return user


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "username": user["username"],
        "full_name": user["full_name"],
        "role_name": user["role_name"],
        "role_label": user["role_label"],
        "is_admin": user["is_admin"],
        "pages": user["pages"],
    }


def require_admin(request: Request) -> Dict[str, Any]:
    if AUTH_DISABLED:
        return {"username": "local", "is_admin": True}
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Не авторизован")
    if not user["is_admin"]:
        raise HTTPException(403, "Требуются права администратора")
    return user


def require_data_console(request: Request) -> Dict[str, Any]:
    """Как у остальной платформы: при AUTH_DISABLED=1 вход не нужен, при включённом входе — только администратор."""
    data_console.check_request(request)
    return require_admin(request)


app.include_router(data_console.router, dependencies=[Depends(require_data_console)])


def _no_cache(path: str, response):
    """Страницы/скрипты дашборда браузер должен перепроверять каждый раз,
    иначе после обновления кода показывается старая закэшированная версия."""
    if path.startswith("/dashboard/") or path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Один гейт перед всем приложением — до любой страницы и любого API-ответа.

    Порядок регистрации middleware важен: SessionMiddleware добавлен НИЖЕ по
    файлу (после этого декоратора), поэтому оборачивает auth_gate снаружи —
    request.session уже заполнен из подписанной cookie к моменту, когда этот
    код его читает (Starlette оборачивает в обратном порядке регистрации)."""
    path = request.url.path

    # Вход и роли выключены (по умолчанию); AUTH_DISABLED=0 включает их.
    if AUTH_DISABLED:
        return _no_cache(path, await call_next(request))

    if path in auth.PUBLIC_PATHS:
        return await call_next(request)

    is_api = path.startswith("/api/")
    user = _current_user(request)

    if not user:
        if is_api:
            return JSONResponse({"detail": "Не авторизован"}, status_code=401)
        return RedirectResponse(url=f"/dashboard/login.html?next={path}", status_code=303)

    if path.startswith("/admin") or path == "/dashboard/data-console.html":
        if not user["is_admin"]:
            if is_api:
                return JSONResponse({"detail": "Доступ запрещён"}, status_code=403)
            return RedirectResponse(url="/dashboard/index.html?denied=1", status_code=303)
        return await call_next(request)

    if user["is_admin"] or path in auth.FREE_HTML_PATHS:
        return _no_cache(path, await call_next(request))

    page_key = auth.resolve_page_key(path)
    if page_key is None:
        # Не размечено (например статический .js/.css) — реальные данные
        # защищены на уровне /api/*, отдачу самого файла не гейтим отдельно.
        return _no_cache(path, await call_next(request))

    if page_key in (user.get("pages") or []):
        return _no_cache(path, await call_next(request))

    if is_api:
        return JSONResponse({"detail": "Доступ запрещён"}, status_code=403)
    return RedirectResponse(url="/dashboard/index.html?denied=1", status_code=303)


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="bip_session",
    max_age=60 * 60 * 24 * 7,  # 7 дней
)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login", summary="Вход")
async def login(payload: LoginRequest, request: Request):
    user = auth.authenticate(payload.username, payload.password)
    if not user:
        raise HTTPException(401, "Неверный логин или пароль")
    request.session.clear()
    request.session["user_id"] = user["id"]
    return {"ok": True, "user": _public_user(user)}


@app.post("/api/auth/logout", summary="Выход")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me", summary="Текущий пользователь")
async def me(request: Request):
    if AUTH_DISABLED:
        return {"username": "local", "full_name": None, "role_name": "admin", "role_label": "Вход выключен",
                "is_admin": True, "pages": list(auth.PAGE_KEYS), "auth_disabled": True}
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Не авторизован")
    return _public_user(user)


@app.get("/api/admin/pages", summary="Функциональные блоки для чекбоксов ролей")
async def admin_list_pages(_: Dict[str, Any] = Depends(require_admin)):
    return {"pages": [{"key": p["key"], "label": p["label"]} for p in auth.PAGE_DEFINITIONS]}


@app.get("/api/admin/users", summary="Список пользователей")
async def admin_list_users(_: Dict[str, Any] = Depends(require_admin)):
    return {"users": auth.list_users()}


class CreateUserRequest(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    role_id: int


@app.post("/api/admin/users", summary="Создать пользователя")
async def admin_create_user(payload: CreateUserRequest, _: Dict[str, Any] = Depends(require_admin)):
    try:
        return auth.create_user(payload.username, payload.password, payload.full_name, payload.role_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


class UpdateUserRequest(BaseModel):
    full_name: Optional[str] = None
    role_id: Optional[int] = None
    is_active: Optional[bool] = None
    new_password: Optional[str] = None


@app.patch("/api/admin/users/{user_id}", summary="Изменить пользователя")
async def admin_update_user(user_id: int, payload: UpdateUserRequest, _: Dict[str, Any] = Depends(require_admin)):
    try:
        return auth.update_user(user_id, payload.full_name, payload.role_id, payload.is_active, payload.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/admin/users/{user_id}", summary="Удалить пользователя")
async def admin_delete_user(user_id: int, _: Dict[str, Any] = Depends(require_admin)):
    try:
        auth.delete_user(user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/admin/roles", summary="Список ролей")
async def admin_list_roles(_: Dict[str, Any] = Depends(require_admin)):
    return {"roles": auth.list_roles()}


class CreateRoleRequest(BaseModel):
    name: str
    label: str
    pages: List[str] = []


@app.post("/api/admin/roles", summary="Создать роль")
async def admin_create_role(payload: CreateRoleRequest, _: Dict[str, Any] = Depends(require_admin)):
    try:
        return auth.create_role(payload.name, payload.label, payload.pages)
    except ValueError as e:
        raise HTTPException(400, str(e))


class UpdateRoleRequest(BaseModel):
    label: Optional[str] = None
    pages: Optional[List[str]] = None


@app.patch("/api/admin/roles/{role_id}", summary="Изменить роль")
async def admin_update_role(role_id: int, payload: UpdateRoleRequest, _: Dict[str, Any] = Depends(require_admin)):
    try:
        return auth.update_role(role_id, payload.label, payload.pages)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/admin/roles/{role_id}", summary="Удалить роль")
async def admin_delete_role(role_id: int, _: Dict[str, Any] = Depends(require_admin)):
    try:
        auth.delete_role(role_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ═══════════════════════════════════════════════════════════
# ATM — СПИСОК / ДЕТАЛИ
# ═══════════════════════════════════════════════════════════

@app.get("/api/atms", summary="Список ATM (с фильтрами)")
async def get_atms(
    region: Optional[str] = Query(None, description="Область"),
    branch: Optional[str] = Query(None, description="Филиал"),
    status: Optional[str] = Query(None, pattern="^(ok|warning|critical|unknown)$"),
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    rows = list_atms(region=region, branch=branch, status=status, limit=limit, offset=offset)
    return {
        "atms": rows,
        "count": len(rows),
        "total_in_db": count_atms(),
        "filters": {"region": region, "branch": branch, "status": status},
    }


@app.get("/api/atms/stats", summary="Статистика по областям/филиалам")
async def get_stats():
    return {
        "total": count_atms(),
        "by_region": list_regions(),
        "by_branch": list_branches(),
    }


@app.get("/api/atms/{terminal_id}", summary="Детали ATM по Terminal ID")
async def get_atm_detail(terminal_id: str):
    atm = get_atm(terminal_id)
    if not atm:
        raise HTTPException(404, f"ATM с terminal_id={terminal_id!r} не найден")
    return atm


# ═══════════════════════════════════════════════════════════
# КАССЕТЫ + BASELINE
# ═══════════════════════════════════════════════════════════

CASSETTE_MAX_COUNT = 2000  # физический потолок купюр на одну кассету (оценка без реальных данных)


@app.get("/api/atms/{terminal_id}/cassettes", summary="Кассеты + рекомендация по загрузке")
async def get_atm_cassettes(terminal_id: str):
    """
    Если ATM опрашивается сборщиком atm_monitor — отдаём реальные кассеты
    (номинал/количество/статус OK-LOW-MISSING на момент последнего опроса).
    Иначе — оценка: текущий остаток разбивается на 4 номинала пропорционально
    типичной доле в обороте (source="estimated", это не измерение).
    """
    atm = get_atm(terminal_id)
    if not atm:
        raise HTTPException(404, f"ATM {terminal_id!r} не найден")

    # get_atm() уже подмешал реальные кассеты (см. core.db._apply_live_state);
    # отдельный запрос к atm_monitor тут не нужен.
    live_cassettes = atm.get("cassettes") or []
    if live_cassettes:
        return {
            "atm_id": terminal_id,
            "address": atm.get("address"),
            "region": atm.get("region"),
            "branch": atm.get("branch"),
            "current_balance": atm.get("balance"),
            "balance_polled_at": atm.get("balance_polled_at"),
            "source": "live",
            "cassettes": live_cassettes,
        }

    balance = atm.get("balance")
    capacity = atm.get("capacity") or 400_000_000

    if balance is None:
        return {
            "atm_id": terminal_id,
            "address": atm.get("address"),
            "region": atm.get("region"),
            "branch": atm.get("branch"),
            "current_balance": None,
            "capacity": capacity,
            "source": "estimated",
            "cassettes": {
                "denominations": [10_000, 50_000, 100_000, 200_000],
                "by_denom": {
                    str(d): {"count": 0, "balance": 0, "capacity": CASSETTE_MAX_COUNT, "fill_pct": 0}
                    for d in (10_000, 50_000, 100_000, 200_000)
                },
                "total_balance": 0,
                "total_fill_pct": 0,
                "value_to_fill": capacity,
            },
            "comment": "Баланс не загружен. Обновите через /api/atms/{id}/balance.",
        }

    # Оценка (нет реальных кассет от atm_monitor): раскладываем известный остаток
    # по 4 номиналам пропорционально типичной доле в обороте, но не больше
    # CASSETTE_MAX_COUNT купюр в одной кассете — это физический потолок реальной
    # кассеты. Итоговый баланс кассет — не сам исходный остаток, а честная сумма
    # «количество купюр × номинал» после этого ограничения.
    share = {10_000: 0.10, 50_000: 0.45, 100_000: 0.30, 200_000: 0.15}
    by_denom: Dict[str, Dict[str, Any]] = {}
    for d, s in share.items():
        alloc = int(balance * s)
        count = min(alloc // d, CASSETTE_MAX_COUNT)
        by_denom[str(d)] = {
            "count": int(count),
            "balance": int(count * d),
            "capacity": CASSETTE_MAX_COUNT,
            "fill_pct": round(count / CASSETTE_MAX_COUNT * 100, 1),
        }

    total_cassette_value = sum(int(c["balance"]) for c in by_denom.values())
    value_to_fill = max(0, capacity - total_cassette_value)

    return {
        "atm_id": terminal_id,
        "address": atm.get("address"),
        "region": atm.get("region"),
        "branch": atm.get("branch"),
        "current_balance": balance,
        "capacity": capacity,
        "balance_pct": atm.get("balance_pct"),
        "status": atm.get("status"),
        "source": "estimated",
        "cassettes": {
            "denominations": [10_000, 50_000, 100_000, 200_000],
            "by_denom": by_denom,
            "total_balance": total_cassette_value,
            "total_fill_pct": round(total_cassette_value / capacity * 100, 1) if capacity else 0,
            "value_to_fill": value_to_fill,
        },
        "refill_needed": value_to_fill,
    }


@app.get("/api/atms/{terminal_id}/history", summary="Реальная история остатка ATM (для прогноза)")
async def get_atm_history(
    terminal_id: str,
    days: int = Query(30, ge=1, le=180, description="Сколько дней истории отдать"),
):
    """
    Реальный почасовой ряд остатка UZS из atm_monitor (сборщик monitoring.btech.uz).
    Пусто, если этот ATM не опрашивается сборщиком или он сейчас недоступен.
    """
    atm = get_atm(terminal_id)
    live_tid = (atm.get("live_tid") if atm else None) or terminal_id
    history = atm_live.balance_history_by_tid(live_tid, days=days)
    return {"terminal_id": terminal_id, "days": days, "history": history, "count": len(history)}


# ═══════════════════════════════════════════════════════════
# ИМПОРТ ATM (XLSX)
# ═══════════════════════════════════════════════════════════

@app.post("/api/atms/import", summary="Импорт ATM из XLSX")
async def import_atms(
    file: UploadFile = File(..., description="XLSX с реестром ATM"),
    replace: bool = Query(False, description="Если True — очистить таблицу перед импортом"),
):
    """
    Ожидаемая структура XLSX:
      A — Номер банкомата, B — Номер по филиалам, C — Локал код, D — Область,
      E — Филиал, F — Модель, G — Тип сети, H — Серийный номер, I — Merchant ID,
      J — Terminal ID, K — Адрес, L — Широта, M — Долгота
    """
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Ожидается .xlsx файл")

    tmp_dir = tempfile.mkdtemp(prefix="atm_import_")
    tmp_path = os.path.join(tmp_dir, _safe_upload_name(file))
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            parsed = parse_xlsx(tmp_path)
        except _PARSE_ERRORS as e:
            raise HTTPException(400, _parse_error_text(e))

        deleted = truncate_atms() if replace else 0
        stats = bulk_insert_atms(parsed["records"])

        return {
            "ok": True,
            "filename": file.filename,
            "header_row": parsed["header_row"],
            "columns_detected": parsed["columns"],
            "total_rows_in_file": parsed["total_rows"],
            "deleted_before_import": deleted,
            "imported": stats["inserted"],
            "updated": stats["updated"],
            "skipped_no_terminal_id": stats["skipped"],
            "validation_errors": parsed["errors"][:50],
            "validation_errors_count": len(parsed["errors"]),
            "atms_in_db_after": count_atms(),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/atms/import/clear", summary="Очистить все ATM из БД")
async def clear_atms():
    deleted = truncate_atms()
    return {"ok": True, "deleted": deleted, "atms_in_db": count_atms()}


# ═══════════════════════════════════════════════════════════
# ФИЛИАЛЫ
# ═══════════════════════════════════════════════════════════

@app.get("/api/branches", summary="Список филиалов (с фильтрами)")
async def get_branches(
    region: Optional[str] = Query(None, description="Регион"),
    incassation: Optional[int] = Query(None, ge=0, le=1, description="0 — без инкассации, 1 — с инкассацией"),
    limit: int = Query(2000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    with_balance: bool = Query(True, description="Прикрепить кассовый остаток, если загружен"),
):
    rows = list_branches_full(region=region, incassation=incassation, limit=limit, offset=offset)
    vash_counts = vash_counts_by_local_code()
    for b in rows:
        b["vash_count"] = vash_counts.get(str(b.get("local_code") or ""), 0)
    if with_balance:
        bal_map = balances_by_local_code_map()
        forecasts = get_all_branch_forecasts()
        for b in rows:
            code = str(b.get("local_code") or "")
            if code and code in bal_map:
                cash = dict(bal_map[code])
                if code in forecasts:
                    cash["forecast"] = forecasts[code]
                b["cash"] = cash
        apply_ledger_to_branches(rows)
    return {
        "branches": rows,
        "count": len(rows),
        "total_in_db": count_branches(),
        "filters": {"region": region, "incassation": incassation},
    }


@app.get("/api/branches/stats", summary="Статистика по филиалам")
async def get_branches_stats():
    all_branches = list_branches_full(limit=5000)
    with_inc = sum(1 for b in all_branches if b.get("incassation") == 1)
    regions: Dict[str, int] = {}
    for b in all_branches:
        r = b.get("region") or "Не указан"
        regions[r] = regions.get(r, 0) + 1
    return {
        "total": count_branches(),
        "with_incassation": with_inc,
        "without_incassation": len(all_branches) - with_inc,
        "by_region": [{"region": r, "count": c} for r, c in sorted(regions.items(), key=lambda x: -x[1])],
    }


@app.get("/api/branches/{local_code}", summary="Детали филиала по локал коду")
async def get_branch_detail(local_code: str):
    branch = get_branch(local_code)
    if not branch:
        raise HTTPException(404, f"Филиал с local_code={local_code!r} не найден")
    cash = get_branch_balance_by_local_code(local_code)
    if cash:
        branch["cash"] = cash
    apply_ledger_to_branches([branch])
    return branch


@app.get("/api/branches/{local_code}/balance", summary="Кассовый остаток филиала")
async def get_branch_cash_balance(local_code: str):
    cash = get_branch_balance_by_local_code(local_code)
    if not cash:
        raise HTTPException(404, f"Остаток для филиала {local_code!r} не найден — загрузите Excel остатков")
    return cash


@app.get("/api/branch-balances", summary="Все загруженные остатки филиалов")
async def get_all_branch_balances():
    rows = list_branch_balances()
    return {"balances": rows, "count": len(rows)}


@app.get("/api/branch-cash/limits", summary="Лимиты кассы филиалов (сўм и доллар)")
async def get_cash_limits():
    return {"limits": list_editable_limits()}


@app.put("/api/branch-cash/limits/{local_code}", summary="Изменить лимит филиала")
async def update_cash_limit(local_code: str, body: Dict[str, Any] = Body(...)):
    def _num(key: str):
        if key not in body or body[key] is None or body[key] == "":
            return None
        try:
            return float(body[key])
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} должно быть числом")
    try:
        saved = save_cash_limit(local_code, _num("limit_uzs"), _num("limit_usd"))
    except LookupError:
        raise HTTPException(404, "Филиал не найден")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **saved}


@app.get("/api/branch-balances/analytics", summary="Сводка кассы из отчёта счетов и сохранённых лимитов")
async def get_branch_cash_analytics(
    import_id: Optional[int] = Query(None, description="ID загрузки отчёта (пусто — последняя)"),
    month: Optional[str] = Query(None, description="YYYY-MM"),
    oper_day: Optional[str] = Query(None, description="YYYY-MM-DD"),
):
    try:
        return ledger_cash_overview(import_id, oper_day, month)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Импорт не найден.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/branch-balances/history", summary="История остатков по филиалам (снимок на каждый импорт Excel)")
async def get_branch_balance_history_endpoint(
    local_code: Optional[str] = Query(None, description="Фильтр по коду филиала"),
):
    rows = get_branch_balance_history(local_code)
    return {"history": rows, "count": len(rows)}


@app.get("/api/sqb-rates", summary="SQB xarid/sotuv kurslari")
async def get_sqb_rates():
    return latest_rates()


@app.post("/api/sqb-rates/import", summary="SQB kurslarini Exceldan yuklash (xarid/sotuv)")
async def import_sqb_rates(
    file: UploadFile = File(..., description="XLSX: sana, Valyuta nomi, Xarid, Sotuv"),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Ожидается .xlsx файл")
    tmp_dir = tempfile.mkdtemp(prefix="sqb_rates_")
    tmp_path = os.path.join(tmp_dir, _safe_upload_name(file))
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            parsed = parse_sqb_rates_xlsx(tmp_path)
        except _PARSE_ERRORS as e:
            raise HTTPException(400, _parse_error_text(e))
        stats = replace_sqb_rates(parsed["records"])
        return {
            "ok": True,
            "filename": file.filename,
            "header_row": parsed["header_row"],
            "dates": parsed["dates"],
            "imported": stats["saved"],
            "validation_errors": parsed["errors"][:50],
            "validation_errors_count": len(parsed["errors"]),
            "current": latest_rates(),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/branches/balances/import", summary="Импорт кассовых остатков филиалов (отдельный XLSX)")
async def import_branch_balances(
    file: UploadFile = File(..., description="XLSX: Код БХМ, номи, Сўм, лимиты, валюты"),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Ожидается .xlsx файл")

    tmp_dir = tempfile.mkdtemp(prefix="branch_bal_import_")
    tmp_path = os.path.join(tmp_dir, _safe_upload_name(file))
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            parsed = parse_branch_balances_xlsx(tmp_path)
        except _PARSE_ERRORS as e:
            raise HTTPException(400, _parse_error_text(e))
        stats = replace_branch_balances(parsed["records"], file.filename)
        return {
            "ok": True,
            "filename": file.filename,
            "import_id": stats["import_id"],
            "label": stats["label"],
            "header_row": parsed["header_row"],
            "columns_detected": parsed["columns"],
            "total_rows_in_file": parsed["total_rows"],
            "imported": stats["saved"],
            "matched_to_branches": stats["matched_to_branches"],
            "unmatched": stats["unmatched"],
            "validation_errors": parsed["errors"][:50],
            "validation_errors_count": len(parsed["errors"]),
            "note": "Отдельный парсер — реестр филиалов (координаты) не изменяется.",
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/api/branches/balances/imports", summary="Список загрузок остатков филиалов")
async def get_branch_balance_imports():
    return {"imports": list_dataset_imports(KIND_BALANCES)}


@app.delete("/api/branches/balances/imports/{import_id}", summary="Удалить одну загрузку остатков филиалов")
async def remove_branch_balance_import(import_id: int):
    if not delete_dataset_import(KIND_BALANCES, import_id):
        raise HTTPException(status_code=404, detail="Импорт не найден.")
    return {"ok": True}


@app.post("/api/branches/balances/clear", summary="Очистить все кассовые остатки филиалов")
async def clear_branch_cash_balances():
    deleted = clear_branch_balances()
    return {"ok": True, "deleted": deleted, "balances_in_db": 0}


def _save_upload(file: UploadFile, prefix: str) -> tuple[str, str]:
    tmp_dir = tempfile.mkdtemp(prefix=prefix)
    tmp_path = os.path.join(tmp_dir, _safe_upload_name(file))
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return tmp_dir, tmp_path


@app.get("/api/branch-ledger/imports", summary="Список загрузок отчёта остатков")
async def get_branch_ledger_imports():
    return {"imports": list_branch_ledger_imports()}


@app.delete("/api/branch-ledger/imports/{import_id}", summary="Удалить одну загрузку отчёта остатков")
async def remove_branch_ledger_import(import_id: int):
    if not delete_branch_ledger_import(import_id):
        raise HTTPException(status_code=404, detail="Импорт не найден.")
    return {"ok": True}


@app.get("/api/branch-ledger/periods", summary="Дни и месяцы внутри выбранной загрузки отчёта")
async def get_branch_ledger_periods(
    import_id: Optional[int] = Query(None, description="ID загрузки (пусто — последняя)"),
):
    try:
        return ledger_import_periods(import_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Импорт не найден.")


@app.get("/api/branch-ledger/analytics", summary="Остатки счетов филиалов на выбранную дату/месяц")
async def get_branch_ledger_analytics(
    branch_id: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    import_id: Optional[int] = Query(None, description="ID загрузки (пусто — последняя)"),
    month: Optional[str] = Query(None, description="YYYY-MM — последний день месяца в этом импорте"),
    oper_day: Optional[str] = Query(None, description="YYYY-MM-DD — конкретный oper_day"),
):
    try:
        return branch_ledger_analytics(branch_id, limit, import_id, oper_day, month)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Импорт не найден.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/vash/operations/periods", summary="Дни и месяцы внутри выбранной загрузки ВАШ")
async def get_vash_operation_periods(
    import_id: Optional[int] = Query(None, description="ID загрузки (пусто — последняя)"),
):
    try:
        return vash_import_periods(import_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Импорт не найден.")


@app.post("/api/branch-ledger/import", summary="Импорт отчёта остатков: oper_day, coa_code, code, code_currency, acc_id, branch_id, saldo_out")
async def import_branch_ledger(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Ожидается .xlsx файл")
    tmp_dir, tmp_path = _save_upload(file, "branch_ledger_")
    try:
        try:
            parsed = parse_branch_ledger_xlsx(tmp_path)
        except _PARSE_ERRORS as e:
            raise HTTPException(400, _parse_error_text(e))
        stats = replace_branch_ledger(parsed["records"], file.filename)
        return {
            "ok": True,
            "filename": file.filename,
            "import_id": stats["import_id"],
            "label": stats["label"],
            "header_row": parsed["header_row"],
            "imported": stats["saved"],
            "days": stats["days"],
            "branches": stats["branches"],
            "validation_errors": parsed["errors"][:50],
            "validation_errors_count": len(parsed["errors"]),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/branch-ledger/clear", summary="Очистить отчёт остатков счетов")
async def clear_branch_ledger_endpoint():
    return {"ok": True, "deleted": clear_branch_ledger()}


@app.get("/api/vash/operations/imports", summary="Список загрузок операций ВАШ")
async def get_vash_operation_imports():
    return {"imports": list_vash_operation_imports()}


@app.delete("/api/vash/operations/imports/{import_id}", summary="Удалить одну загрузку операций ВАШ")
async def remove_vash_operation_import(import_id: int):
    if not delete_vash_operation_import(import_id):
        raise HTTPException(status_code=404, detail="Импорт не найден.")
    return {"ok": True}


@app.get("/api/vash/operations/analytics", summary="Где операций ВАШ много и где мало")
async def get_vash_operations_analytics(
    import_id: Optional[int] = Query(None, description="ID загрузки (пусто — последняя)"),
    month: Optional[str] = Query(None, description="YYYY-MM — только операции этого месяца"),
    oper_day: Optional[str] = Query(None, description="YYYY-MM-DD — один день"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD — с даты"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD — по дату"),
):
    try:
        return vash_operations_analytics(import_id, oper_day, month, date_from, date_to)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Импорт не найден.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class VashExportPoint(BaseModel):
    obmen_punkt: Optional[str] = None
    vash_number: Optional[str] = None
    address: Optional[str] = None
    branch: Optional[str] = None
    bxm_name: Optional[str] = None
    ops: int = 0
    band: Optional[str] = None
    ci_accounts: int = 0
    co_accounts: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    on_map: bool = False
    lat: Optional[float] = None
    lon: Optional[float] = None


class VashExportRequest(BaseModel):
    points: List[VashExportPoint]
    import_label: Optional[str] = None
    period: Optional[str] = None
    band_label: Optional[str] = None
    search: Optional[str] = None


@app.post("/api/vash/operations/export", summary="Excel: выбранные пункты ВАШ из аналитики")
def export_vash_operations(payload: VashExportRequest):
    try:
        content = export_vash_operations_xlsx(
            [p.model_dump() for p in payload.points],
            import_label=payload.import_label,
            period=payload.period,
            band_label=payload.band_label,
            search=payload.search,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="VASH_points_{stamp}.xlsx"'},
    )


@app.post("/api/vash/operations/import", summary="Импорт операций ВАШ: ci_acc, co_acc, obmen_punkt, branch, date")
async def import_vash_operations(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Ожидается .xlsx файл")
    tmp_dir, tmp_path = _save_upload(file, "vash_ops_")
    try:
        try:
            parsed = parse_vash_operations_xlsx(tmp_path)
        except _PARSE_ERRORS as e:
            raise HTTPException(400, _parse_error_text(e))
        stats = replace_vash_operations(parsed["records"], file.filename)
        return {
            "ok": True,
            "filename": file.filename,
            "import_id": stats["import_id"],
            "label": stats["label"],
            "header_row": parsed["header_row"],
            "imported": stats["saved"],
            "days": stats["days"],
            "points": stats["points"],
            "validation_errors": parsed["errors"][:50],
            "validation_errors_count": len(parsed["errors"]),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/vash/operations/clear", summary="Очистить операции ВАШ")
async def clear_vash_operations_endpoint():
    return {"ok": True, "deleted": clear_vash_operations()}


@app.get("/api/cash-equipment", summary="Кассовая техника (kassa jihozlari)")
async def get_cash_equipment(local_code: Optional[str] = Query(None, description="Локал код филиала")):
    rows = list_cash_equipment(local_code)
    return {"items": rows, "count": len(rows)}


@app.get("/api/cash-equipment/branches", summary="Кассовая техника по филиалам (для карты)")
async def get_cash_equipment_branches():
    return cash_equipment_by_branch()


@app.get("/api/cash-equipment/replacement", summary="План замены: износ и дата списания техники")
async def get_cash_equipment_replacement(
    kind: Optional[str] = Query("Mashinka", description="Лист Excel (вид техники); пусто — вся техника"),
):
    return equipment_replacement_plan(kind or None)


@app.post("/api/cash-equipment/import", summary="Импорт списка кассовой техники (XLSX)")
async def import_cash_equipment(
    file: UploadFile = File(..., description="XLSX: локал код, бўлинма номи, асосий восита номи, инвентар рақами …"),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Ожидается .xlsx файл")
    tmp_dir = tempfile.mkdtemp(prefix="cash_eq_import_")
    tmp_path = os.path.join(tmp_dir, _safe_upload_name(file))
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            parsed = parse_cash_equipment_xlsx(tmp_path)
        except _PARSE_ERRORS as e:
            raise HTTPException(400, _parse_error_text(e))
        stats = replace_cash_equipment(parsed["records"])
        return {
            "ok": True,
            "filename": file.filename,
            "by_sheet": parsed["by_sheet"],
            "total_rows_in_file": parsed["total_rows"],
            "imported": stats["saved"],
            "matched_to_branches": stats["matched"],
            "unmatched": stats["unmatched"],
            "branches_matched": stats["branches_matched"],
            "validation_errors": parsed["errors"][:50],
            "validation_errors_count": len(parsed["errors"]),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/cash-equipment/clear", summary="Очистить кассовую технику")
async def clear_cash_equipment_endpoint():
    return {"ok": True, "deleted": clear_cash_equipment()}


@app.get("/api/vash", summary="Сколько ВАШ загружено (последний импорт)")
async def get_vash_status():
    return {"count": count_vash_zones()}


@app.get("/api/vash/imports", summary="Список загрузок шохобча ВАШ")
async def get_vash_zone_imports():
    return {"imports": list_vash_zone_imports()}


@app.delete("/api/vash/imports/{import_id}", summary="Удалить одну загрузку шохобча ВАШ")
async def remove_vash_zone_import(import_id: int):
    if not delete_vash_zone_import(import_id):
        raise HTTPException(status_code=404, detail="Импорт не найден.")
    return {"ok": True}


@app.get("/api/vash/branches", summary="ВАШ (валюта айирбошлаш шохобчалари) по филиалам — для карты")
async def get_vash_branches(
    import_id: Optional[int] = Query(None, description="ID загрузки (пусто — последняя)"),
):
    try:
        return vash_by_branch(import_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Импорт не найден.")


@app.post("/api/vash/import", summary="Импорт списка ВАШ (XLSX)")
async def import_vash(
    file: UploadFile = File(..., description="XLSX: №, ВАШ рақами, БХМ коди, БХМ номи, манзил, локация"),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Ожидается .xlsx файл")
    tmp_dir = tempfile.mkdtemp(prefix="vash_import_")
    tmp_path = os.path.join(tmp_dir, _safe_upload_name(file))
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            parsed = parse_vash_xlsx(tmp_path, known_branch_codes())
        except _PARSE_ERRORS as e:
            raise HTTPException(400, _parse_error_text(e))
        stats = replace_vash_zones(parsed["records"], file.filename)
        grouped = vash_by_branch(stats["import_id"])
        return {
            "ok": True,
            "filename": file.filename,
            "import_id": stats["import_id"],
            "label": stats["label"],
            "sheets": parsed["sheets"],
            "imported": stats["saved"],
            "matched_to_branches": stats["matched"],
            "unmatched": stats["unmatched"],
            "branches_with_vash": stats["branches_matched"],
            "with_coords": stats["with_coords"],
            "without_coords": stats["saved"] - stats["with_coords"],
            "coords_swapped_fixed": parsed["coords_swapped"],
            "coords_coarse": parsed["coords_coarse"],
            "skipped_total_rows": parsed["skipped_total_rows"],
            "per_branch": [
                {"local_code": g["local_code"], "name": g["branch_name"], "region": g["region"], "count": g["count"],
                 "with_coords": g["with_coords"]}
                for g in grouped["branches"]
            ],
            "unmatched_branches": [
                {"bxm_code": g["bxm_code"], "bxm_name": g["bxm_name"], "count": g["count"]}
                for g in grouped["unmatched"]
            ],
            "validation_errors": parsed["errors"][:200],
            "validation_errors_count": len(parsed["errors"]),
            "warnings": parsed["warnings"][:100],
            "warnings_count": len(parsed["warnings"]),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/vash/clear", summary="Очистить список ВАШ")
async def clear_vash_endpoint():
    return {"ok": True, "deleted": clear_vash_zones()}


@app.post("/api/branches/import", summary="Импорт филиалов из XLSX")
async def import_branches(
    file: UploadFile = File(..., description="XLSX с реестром филиалов"),
    replace: bool = Query(False, description="Если True — очистить таблицу перед импортом"),
):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Ожидается .xlsx файл")

    tmp_dir = tempfile.mkdtemp(prefix="branch_import_")
    tmp_path = os.path.join(tmp_dir, _safe_upload_name(file))
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            parsed = parse_branches_xlsx(tmp_path)
        except _PARSE_ERRORS as e:
            raise HTTPException(400, _parse_error_text(e))

        deleted = truncate_branches() if replace else 0
        stats = bulk_insert_branches(parsed["records"])
        without_coordinates = parsed.get("without_coordinates", 0)
        warning = None
        if without_coordinates:
            warning = (
                f"У {without_coordinates} из {len(parsed['records'])} филиалов нет координат "
                "(lat/lon) — на карте они не отобразятся. Проверьте колонки широты и долготы "
                "и загрузите файл снова с галочкой «Очистить таблицу»."
            )

        return {
            "ok": True,
            "filename": file.filename,
            "header_row": parsed["header_row"],
            "columns_detected": parsed["columns"],
            "total_rows_in_file": parsed["total_rows"],
            "deleted_before_import": deleted,
            "imported": stats["inserted"],
            "updated": stats["updated"],
            "skipped_no_local_code": stats["skipped"],
            "without_coordinates": without_coordinates,
            "warning": warning,
            "validation_errors": parsed["errors"][:50],
            "validation_errors_count": len(parsed["errors"]),
            "branches_in_db_after": count_branches(),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/branches/import/clear", summary="Очистить все филиалы из БД")
async def clear_branches():
    deleted = truncate_branches()
    return {"ok": True, "deleted": deleted, "branches_in_db": count_branches()}


# ═══════════════════════════════════════════════════════════
# БАЛАНСЫ
# ═══════════════════════════════════════════════════════════

class BalanceUpdate(BaseModel):
    balance: int


class BulkBalances(BaseModel):
    updates: List[Dict[str, Any]]   # [{"terminal_id": "...", "balance": 123}, ...]


@app.post("/api/atms/{terminal_id}/balance", summary="Обновить остаток ATM")
async def set_balance(terminal_id: str, payload: BalanceUpdate):
    if payload.balance < 0:
        raise HTTPException(400, "balance должен быть >= 0")
    if not update_balance(terminal_id, payload.balance):
        raise HTTPException(404, f"ATM {terminal_id!r} не найден")
    return {"ok": True, "atm": get_atm(terminal_id)}


@app.post("/api/atms/balances/bulk", summary="Массовое обновление остатков")
async def set_balances_bulk(payload: BulkBalances):
    return {"ok": True, **bulk_update_balances(payload.updates)}


# ═══════════════════════════════════════════════════════════
# ALERTS / BASELINE
# ═══════════════════════════════════════════════════════════

@app.get("/api/alerts", summary="ATM в статусе critical/warning")
async def get_alerts(limit: int = Query(200, ge=1, le=2000)):
    alerts = [a for a in list_atms(limit=limit) if a.get("status") in ("critical", "warning")]
    alerts.sort(key=lambda x: (x.get("balance_pct") or 999))
    return {"alerts": alerts, "count": len(alerts)}


def _aggregate_by_region(atms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, Any]] = {}
    for a in atms:
        r = a.get("region") or "Не указана"
        bucket = agg.setdefault(r, {
            "region": r, "count": 0, "total_capacity": 0, "total_balance": 0,
            "critical": 0, "warning": 0, "ok": 0, "no_data": 0,
        })
        bucket["count"] += 1
        bucket["total_capacity"] += a.get("capacity") or 0
        bucket["total_balance"] += a.get("balance") or 0
        st = a.get("status")
        if st in ("critical", "warning", "ok"):
            bucket[st] += 1
        else:
            bucket["no_data"] += 1
    out = []
    for r in agg.values():
        r["fill_pct"] = round(r["total_balance"] / (r["total_capacity"] or 1) * 100, 1)
        out.append(r)
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


@app.get("/api/baseline", summary="Сводный отчёт по остаткам")
async def get_baseline():
    all_atms = list_atms(limit=5000)
    total_capacity = sum(a.get("capacity") or 0 for a in all_atms)
    total_balance = sum(a.get("balance") or 0 for a in all_atms)
    return {
        "summary": {
            "total_atms": len(all_atms),
            "total_capacity": total_capacity,
            "total_balance": total_balance,
            "fill_pct": round(total_balance / total_capacity * 100, 1) if total_capacity else 0,
            "no_data": sum(1 for a in all_atms if a.get("balance") is None),
            "critical": sum(1 for a in all_atms if a.get("status") == "critical"),
            "warning": sum(1 for a in all_atms if a.get("status") == "warning"),
            "ok": sum(1 for a in all_atms if a.get("status") == "ok"),
        },
        "by_region": _aggregate_by_region(all_atms),
    }


# ═══════════════════════════════════════════════════════════
# ИНКАССАЦИЯ
# ═══════════════════════════════════════════════════════════

class IncassationRouteRequest(BaseModel):
    persist: bool = True


@app.post("/api/routes/incassation", summary="Маршруты: только ATM ниже нормы, свой вилоят")
async def regional_incassation_route(
    status: str = Query("warning", pattern="^(critical|warning|all)$"),
    speed_kmh: int = Query(30, ge=10, le=80),
    max_stops: int = Query(12, ge=1, le=200),
    snap_roads: bool = Query(True),
    payload: Optional[IncassationRouteRequest] = Body(None),
):
    req = payload or IncassationRouteRequest()
    atms = list_atms(limit=5000)  # уже с реальным балансом из atm_monitor, см. core.db
    branches = list_branches_full(incassation=1, limit=5000)
    result = build_regional_routes(
        atms, branches, status,
        speed_kmh=speed_kmh, max_stops=max_stops, snap_roads=snap_roads,
    )
    if req.persist:
        result["saved_to_calendar"] = replace_incassation_trips(result.get("cars") or [])
    else:
        result["saved_to_calendar"] = 0
    return result


@app.get("/api/incassation/calendar", summary="Сохранённые рейсы инкассации для календаря")
async def incassation_calendar():
    trips = list_incassation_trips()
    events = []
    for t in trips:
        due = f"{t.get('planned_date')}T09:00:00+05:00"
        events.append({
            "id": t.get("id"),
            "due_at": due,
            "planned_date": t.get("planned_date"),
            "region": t.get("region"),
            "address": t.get("label") or t.get("branch_address"),
            "terminal_id": t.get("branch_local_code"),
            "priority": t.get("priority") or "planned",
            "recommended_refill": t.get("refill_total") or 0,
            "stops": t.get("stops") or [],
            "stop_count": len(t.get("stops") or []),
            "distance_km": t.get("distance_km"),
            "est_time_min": t.get("est_time_min"),
            "label": t.get("label"),
            "branch_address": t.get("branch_address"),
            "branch_lat": t.get("branch_lat"),
            "branch_lon": t.get("branch_lon"),
            "geometry": t.get("geometry") or [],
            "type": "route",
        })
    return {"events": events, "count": len(events), "trips": trips}


@app.get("/api/incassation/trips/{trip_id}", summary="Один сохранённый рейс для карты")
async def incassation_trip(trip_id: int):
    for t in list_incassation_trips():
        if int(t.get("id") or 0) == int(trip_id):
            return t
    raise HTTPException(404, f"Рейс {trip_id} не найден")


class OsrmRouteRequest(BaseModel):
    points: List[Dict[str, float]]


@app.post("/api/osrm/route", summary="Геометрия маршрута по дорогам (OSRM)")
async def osrm_route(payload: OsrmRouteRequest):
    geom, dist = osrm_route_geometry(payload.points)
    if not geom:
        raise HTTPException(502, "OSRM недоступен")
    return {"geometry": geom, "distance_km": round(float(dist or 0), 2), "provider": "osrm"}


@app.get("/api/incassation/plan", summary="Прогнозный план инкассации")
async def incassation_plan(days: int = Query(2, ge=1, le=14)):
    atms = list_atms(limit=5000)
    now = datetime.now(timezone.utc)
    planned = []
    for a in atms:
        hours = hours_to_low_cash(a)
        if hours is None or hours > days * 24:
            continue
        cap, bal = a.get("capacity") or 400_000_000, a.get("balance")
        planned.append({
            "terminal_id": a["terminal_id"],
            "address": a.get("address"),
            "region": a.get("region"),
            "due_at": (now + timedelta(hours=hours)).isoformat(),
            "hours_to_low_cash": hours,
            "priority": "critical" if hours < 12 else "high" if hours < 24 else "planned",
            "recommended_refill": round(max(0, cap * 0.8 - (bal or 0))),
            "forecast_source": a.get("ml_method") if a.get("ml_withdrawal_per_day") is not None else "heuristic",
        })
    planned.sort(key=lambda x: x["hours_to_low_cash"])
    return {
        "generated_at": now.isoformat(),
        "horizon_days": days,
        "planned_atms": planned,
        "count": len(planned),
        "note": (
            "Часы до low-cash — по прогнозу снятий XGBoost (forecast_source), для ATM без прогноза — "
            "оценка по текущему остатку. Календарь рейсов — POST /api/routes/incassation."
        ),
    }


# ═══════════════════════════════════════════════════════════
# ПЛАН ИНКАССАЦИЙ (dashboard/incassation-plan.html) И ЭКОНОМИЯ
# ═══════════════════════════════════════════════════════════

def _who(request: Request) -> Optional[str]:
    user = _current_user(request)
    return user["username"] if user else ("local" if AUTH_DISABLED else None)


@app.get("/api/incassation/queue", summary="Какие инкассации нужны: сумма, номиналы, срок, срочность 1–10, рейсы")
def incassation_queue(
    min_score: int = Query(1, ge=1, le=10),
    route_min_score: int = Query(5, ge=1, le=10),
    max_stops: int = Query(12, ge=1, le=60),
    roads: bool = Query(False, description="Маршрут по дорогам (OSRM) — медленнее"),
):
    return inc_plan.queue_with_routes(min_score, route_min_score, max_stops, roads)


@app.post("/api/incassation/queue/refresh", summary="Пересчитать рекомендации по последним данным")
def incassation_queue_refresh():
    atm_history.safe_process_history()
    return inc_plan.refresh_plan()


class RecStatusRequest(BaseModel):
    status: str  # accepted | open


@app.post("/api/incassation/queue/{rec_id}/status", summary="Принять план в работу / отменить принятие")
def incassation_queue_status(rec_id: int, payload: RecStatusRequest, request: Request):
    try:
        row = inc_plan.set_status(rec_id, payload.status, _who(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not row:
        raise HTTPException(404, "Рекомендация не найдена или уже закрыта")
    return row


@app.get("/api/savings/summary", summary="Экономия: факт банка vs рекомендации системы по реальным циклам инкассации")
def savings_summary_endpoint(days: int = Query(90, ge=7, le=730)):
    return savings.savings_summary(days)


@app.get("/api/savings/cycles", summary="Циклы инкассации с оценкой (по банкомату или по сети)")
def savings_cycles_endpoint(days: int = Query(90, ge=7, le=730), atm_id: Optional[int] = None):
    return {"cycles": savings.compute_cycles(days, atm_id)}


class AIChatRequest(BaseModel):
    messages: List[Dict[str, str]] = []
    days: int = 90
    explain: bool = False


@app.post("/api/savings/ai", summary="ИИ-помощник по экономии: в модель уходят только обезличенный текст и плейсхолдеры")
def savings_ai(payload: AIChatRequest):
    try:
        return ai_assistant.ask(payload.messages, max(7, min(730, payload.days)), payload.explain)
    except ai_assistant.AIError as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/settings/economics", summary="Экономические параметры банка (стоимость денег, выезда…)")
def get_economics():
    return economics.get_settings()


class EconomicsUpdate(BaseModel):
    values: Dict[str, Optional[float]]


@app.put("/api/settings/economics", summary="Изменить экономические параметры (только админ)")
def put_economics(payload: EconomicsUpdate, user: Dict[str, Any] = Depends(require_admin)):
    try:
        result = economics.update_settings(payload.values, user.get("username"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    threading.Thread(target=inc_plan.safe_refresh_plan, daemon=True).start()
    return result


# ═══════════════════════════════════════════════════════════
# АНАЛИТИКА ATM (dashboard/analytics.html)
# ═══════════════════════════════════════════════════════════

@app.get("/api/analytics/atm-status", summary="Состояние сети: работают, заняты, офлайн, не заряжены, неисправности")
def analytics_atm_status():
    return atm_analytics.network_status()


class AtmStatusExportRequest(BaseModel):
    atm_ids: List[int]
    region: Optional[str] = None
    filter_label: Optional[str] = None
    search: Optional[str] = None


@app.post("/api/analytics/atm-status/export", summary="Excel: выбранные банкоматы из аналитики состояния сети")
def analytics_atm_status_export(payload: AtmStatusExportRequest):
    try:
        content = atm_analytics.export_atm_status_xlsx(
            payload.atm_ids,
            region=payload.region,
            filter_label=payload.filter_label,
            search=payload.search,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"ATM_status_{stamp}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/analytics/forecast", summary="Прогноз снятий наличных (XGBoost) и сроки cash-out")
def analytics_forecast():
    return atm_analytics.forecast_overview()


@app.get("/api/analytics/withdrawals", summary="Фактические снятия по дням (по истории остатков BTech)")
def analytics_withdrawals(days: int = Query(30, ge=1, le=180)):
    return atm_analytics.withdrawals_history(days)


@app.get("/api/analytics/atm-efficiency", summary="Спрос в час и лишние деньги в банкоматах (запас сверх нормы)")
def analytics_atm_efficiency(
    days: int = Query(30, ge=7, le=180),
    norm_days: int = Query(atm_analytics.NORM_DAYS, ge=1, le=120),
):
    return atm_analytics.atm_efficiency(days, norm_days)


@app.get("/api/analytics/balance-history", summary="История остатка банкомата (или суммы по сети) по часам")
def analytics_balance_history(atm_id: Optional[int] = None, days: int = Query(30, ge=1, le=180)):
    return atm_analytics.balance_history(atm_id, days)


@app.get("/api/analytics/atm-history/{atm_id}", summary="Вся история банкомата: остаток по часам, снятия по дням")
def analytics_atm_history(atm_id: int):
    return atm_analytics.atm_history(atm_id)


@app.post("/api/analytics/forecast/run", summary="Переобучить модель прогноза сейчас (в фоне)")
def analytics_forecast_run():
    def job():
        atm_forecast.run_forecast()
        atm_analytics.invalidate_cache()
        inc_plan.safe_refresh_plan()

    threading.Thread(target=job, name="atm-forecast-manual", daemon=True).start()
    return {"started": True}


# ═══════════════════════════════════════════════════════════
# CASHIER INTELLIGENCE
# ═══════════════════════════════════════════════════════════

_UNREADABLE_CASHIER_FILE = (
    "Не удалось прочитать файл. Проверьте, что это итоговый Excel-отчёт (.xlsx) "
    "в том же формате, что и предыдущие месяцы."
)


@app.post("/api/cashiers/import", summary="Импорт отчёта KPI кассиров за месяц из XLSX/CSV")
async def import_cashiers(
    file: UploadFile = File(..., description="Excel-отчёт кассиров"),
    period: Optional[str] = Form(None, description="Отчётный месяц ГГГГ-ММ (например 2026-08)"),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Имя файла не указано.")
    if period and not normalize_period(period):
        raise HTTPException(status_code=422, detail="Отчётный месяц должен быть в формате ГГГГ-ММ.")
    period = normalize_period(period) or detect_period(Path(file.filename).stem)
    if not period:
        raise HTTPException(
            status_code=422,
            detail="Не удалось определить отчётный месяц по имени файла. Выберите месяц перед загрузкой.",
        )

    tmp_dir = tempfile.mkdtemp(prefix="cashier_import_")
    tmp_path = Path(tmp_dir) / _safe_upload_name(file)
    try:
        tmp_path.write_bytes(await file.read())
        try:
            parsed = parse_cashiers_xlsx(tmp_path)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception:
            log.exception("Не удалось разобрать отчёт KPI кассиров %s", file.filename)
            raise HTTPException(status_code=422, detail=_UNREADABLE_CASHIER_FILE)
        if not parsed.get("records"):
            raise HTTPException(status_code=422, detail="В файле не найдено ни одной строки кассира.")
        return {"ok": True, "filename": file.filename, **save_cashier_import(file.filename, parsed, period)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/api/cashiers/imports", summary="Список загруженных месяцев KPI кассиров")
async def get_cashier_imports():
    return {"imports": list_cashier_imports()}


@app.get("/api/cashiers/imports/detect-period", summary="Определить отчётный месяц по имени файла")
async def get_cashier_import_period(filename: str = Query(...)):
    return {"period": detect_period(Path(filename).stem)}


@app.delete("/api/cashiers/imports/{import_id}", summary="Удалить загруженный месяц KPI кассиров")
async def remove_cashier_import(import_id: int):
    if not delete_cashier_import(import_id):
        raise HTTPException(status_code=404, detail="Импорт не найден.")
    return {"ok": True}


@app.get("/api/cashiers/analytics", summary="KPI и структура операций кассиров")
async def get_cashier_analytics(
    import_id: Optional[int] = Query(None, description="ID импорта KPI (None = последний)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    role: Optional[str] = Query(None, description="Фильтр по роли: back / front / none (без операций)"),
    search: Optional[str] = Query(None, description="Поиск по ФИО/табелю"),
    position: Optional[str] = Query(None, description="Фильтр по лавозим/должности"),
    status: Optional[str] = Query(None, description="Фильтр по статусу (работает/отпуск/больничный...)"),
    branch: Optional[str] = Query(None, description="Фильтр по филиалу/БХМ"),
):
    return cashier_analytics(import_id, page, page_size, role, search, position, status, branch)


@app.get("/api/cashiers/{report_id}", summary="Углублённая аналитика одного кассира")
async def get_cashier_detail(report_id: int):
    item = cashier_detail(report_id)
    if not item:
        raise HTTPException(status_code=404, detail="Запись кассира не найдена.")
    return item


# ═══════════════════════════════════════════════════════════
# GEOJSON
# ═══════════════════════════════════════════════════════════

def _geojson_response(name: str) -> FileResponse:
    geo_path = BASE_DIR / name
    if not geo_path.exists():
        raise HTTPException(404, "GeoJSON не найден")
    return FileResponse(str(geo_path), media_type="application/geo+json")


@app.get("/tashkent_districts.geojson", summary="Границы районов Ташкента")
async def get_tashkent_geojson():
    return _geojson_response("tashkent_districts.geojson")


@app.get("/uzbekistan_regional.geojson", summary="Границы областей Узбекистана")
async def get_uzb_regional_geojson():
    return _geojson_response("uzbekistan_regional.geojson")


@app.get("/uzbekistan.geojson", summary="Граница Узбекистана")
async def get_uzb_geojson():
    return _geojson_response("uzbekistan.geojson")


# ═══════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8080, reload=False, log_level="info")
