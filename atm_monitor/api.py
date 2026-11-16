"""API страницы «Менеджер банкоматов» (dashboard/atm-monitor.html): текущее
состояние каждого банкомата из БД сборщика (последний снимок) + ручной запуск опроса.
Подключается в api/main.py как router."""
import logging
import threading

from fastapi import APIRouter

from .config import settings
from .daily_history import daily_series
from .db import connect
from .scheduler import run_once

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/atm-monitor", tags=["atm-monitor"])


def _format_forecast(hours) -> "str | None":
    """~159.8 часов -> '~6 д 14 ч' (как на платформе-источнике)."""
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return f"~{int(hours // 24)} д {int(hours % 24)} ч"


@router.get("/atms", summary="Все банкоматы с последним снимком состояния")
def list_atms():
    try:
        with connect() as conn:
            atms = conn.execute("SELECT * FROM atms ORDER BY tid").fetchall()
            snaps = conn.execute(
                """
                SELECT * FROM atm_snapshots
                WHERE id IN (SELECT MAX(id) FROM atm_snapshots GROUP BY atm_id)
                """
            ).fetchall()
            snap_by_atm = {r["atm_id"]: r for r in snaps}

            cassettes: dict = {}
            for c in conn.execute(
                """
                SELECT * FROM cassette_snapshots
                WHERE atm_snapshot_id IN (SELECT MAX(id) FROM atm_snapshots GROUP BY atm_id)
                ORDER BY cassette_index
                """
            ).fetchall():
                cassettes.setdefault(c["atm_snapshot_id"], []).append(c)

            turnover = {
                r["atm_id"]: r
                for r in conn.execute(
                    """
                    SELECT * FROM turnover_snapshots
                    WHERE id IN (SELECT MAX(id) FROM turnover_snapshots GROUP BY atm_id)
                    """
                ).fetchall()
            }
    except Exception:  # noqa: BLE001 — база ещё не создана / пустая
        return []

    result = []
    for atm in atms:
        snap = snap_by_atm.get(atm["id"])
        t = turnover.get(atm["id"])
        result.append(
            {
                "id": atm["id"],
                "tid": atm["tid"],
                "serial": atm["serial"],
                "status": atm["status"],
                "vendor": atm["vendor_name"],
                "model": atm["model_name"],
                "variant": atm["variant_name"],
                "agent_status": snap["agent_status"] if snap else None,
                "cdm_total_uzs": snap["cdm_total_uzs"] if snap else None,
                "forecast": _format_forecast(t["forecast_hours"]) if t else None,
                "polled_at": snap["polled_at"] if snap else None,
                "cassettes": [
                    {
                        "index": c["cassette_index"],
                        "type": c["cassette_type"],
                        "status": c["status"],
                        "count": c["count"],
                        "nominal": c["nominal"],
                    }
                    for c in (cassettes.get(snap["id"], []) if snap else [])
                ],
            }
        )
    return result


@router.post("/poll", summary="Запустить опрос BTech прямо сейчас (в фоне)")
def poll_now():
    threading.Thread(target=run_once, name="atm-monitor-manual", daemon=True).start()
    return {"started": True}


# ── История банкомата (atm_monitor/history.py) ───────────────

@router.get("/atms/{atm_id}/events", summary="Изменения состояния банкомата (только переходы)")
def atm_events(atm_id: int, days: int = 30, kind: "str | None" = None):
    sql = """SELECT id, at, kind, old_value, new_value FROM atm_state_events
             WHERE atm_id = %s AND at >= now() - make_interval(days => %s)"""
    params: list = [atm_id, max(1, min(days, 365))]
    if kind:
        sql += " AND kind LIKE %s"
        params.append(kind.rstrip("*") + "%")
    sql += " ORDER BY at DESC, id DESC LIMIT 2000"
    with connect() as conn:
        return {"atm_id": atm_id, "events": conn.execute(sql, params).fetchall()}


@router.get("/atms/{atm_id}/incassations", summary="Фактические загрузки банкомата")
def atm_incassations(atm_id: int, days: int = 180):
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, at, method, balance_before, available_before, balance_after, available_after,
                      initial_uzs, notes_after, recommendation_id, followed_plan
                 FROM atm_incassations
                WHERE atm_id = %s AND at >= now() - make_interval(days => %s)
                ORDER BY at DESC""",
            (atm_id, max(1, min(days, 730))),
        ).fetchall()
    return {"atm_id": atm_id, "incassations": rows}


@router.get("/atms/{atm_id}/daily", summary="Суточные итоги банкомата")
def atm_daily(atm_id: int, days: int = 90):
    with connect() as conn:
        rows = conn.execute(
            """SELECT day, withdrawn, covered_hours, avg_balance, avg_available, min_available,
                      cashout_hours, offline_hours, samples
                 FROM atm_daily WHERE atm_id = %s AND day >= current_date - %s ORDER BY day""",
            (atm_id, max(1, min(days, 730))),
        ).fetchall()
    return {"atm_id": atm_id, "daily": rows}


# ── Карточка банкомата (dashboard/atm-card.html) ─────────────

@router.get("/card/search", summary="Банкоматы для поиска на странице банкомата")
def card_search():
    with connect() as conn:
        return conn.execute("SELECT id, tid, address, place FROM atms ORDER BY tid").fetchall()


def atm_ids_by_tid(tid: str, terminal: "str | None" = None) -> list:
    """TID в BTech не уникален. Первым идёт запись с тем же terminal_id, затем самая свежая."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT id FROM atms WHERE tid = %s
                ORDER BY (terminal_id IS NOT DISTINCT FROM %s) DESC, last_seen_at DESC NULLS LAST, id DESC""",
            (tid, terminal),
        ).fetchall()
    return [r["id"] for r in rows]


def atm_ids_by_terminal(terminal: str) -> list:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM atms WHERE terminal_id = %s ORDER BY last_seen_at DESC NULLS LAST, id DESC", (terminal,)
        ).fetchall()
    return [r["id"] for r in rows]


def atm_card_data(atm_id: int) -> "dict | None":
    """Всё, что есть в базе по банкомату."""
    with connect() as conn:
        atm = conn.execute("SELECT * FROM atms WHERE id = %s", (atm_id,)).fetchone()
        if not atm:
            return None
        atm = dict(atm)

        def rows(query: str) -> list:
            return conn.execute(query, (atm_id,)).fetchall()

        def one(query: str):
            return conn.execute(query, (atm_id,)).fetchone()

        snapshot = one("SELECT * FROM atm_snapshots WHERE atm_id = %s ORDER BY polled_at DESC, id DESC LIMIT 1")
        # Локация — отдельно: сбой парсинга/реестра не должен ронять всю карточку.
        try:
            from api.core.atm_live import _valid_gps
            import json as _json
            raw = (snapshot or {}).get("raw_json") or {}
            if isinstance(raw, str):
                try:
                    raw = _json.loads(raw)
                except (TypeError, ValueError, _json.JSONDecodeError):
                    raw = {}
            if not isinstance(raw, dict):
                raw = {}
            card = raw.get("card") if isinstance(raw.get("card"), dict) else {}
            gps = card.get("gpsCoords") if isinstance(card.get("gpsCoords"), dict) else {}
            fresh = _valid_gps(gps.get("latitude"), gps.get("longitude")) or _valid_gps(
                atm.get("latitude"), atm.get("longitude")
            )
            if fresh:
                atm["latitude"], atm["longitude"] = fresh
                atm["location_source"] = "btech"
            else:
                atm["latitude"] = atm["longitude"] = None
                atm["location_source"] = None
            if card.get("address"):
                atm["address"] = card.get("address")
            if card.get("place"):
                atm["place"] = card.get("place")
            # Адрес/GPS из Excel-реестра — сверка и fallback при пустом/битом BTech GPS.
            reg = None
            for key in (atm.get("terminal_id"), atm.get("tid"), atm.get("serial")):
                key = str(key or "").strip()
                if not key:
                    continue
                reg = conn.execute(
                    """
                    SELECT address, lat, lon FROM public.atms
                    WHERE TRIM(terminal_id::text) = %s
                       OR TRIM(COALESCE(atm_number::text, '')) = %s
                       OR TRIM(COALESCE(serial::text, '')) = %s
                    LIMIT 1
                    """,
                    (key, key, key),
                ).fetchone()
                if reg:
                    break
            if reg:
                atm["registry_address"] = reg.get("address")
                atm["registry_lat"] = reg.get("lat")
                atm["registry_lon"] = reg.get("lon")
                if not atm.get("latitude"):
                    reg_gps = _valid_gps(reg.get("lat"), reg.get("lon"))
                    if reg_gps:
                        atm["latitude"], atm["longitude"] = reg_gps
                        atm["location_source"] = "registry"
        except Exception:
            log.exception("atm card location enrich failed for atm_id=%s", atm_id)
            try:
                from api.core.atm_live import _valid_gps
                kept = _valid_gps(atm.get("latitude"), atm.get("longitude"))
            except Exception:
                kept = None
            if kept:
                atm["latitude"], atm["longitude"] = kept
            else:
                atm["latitude"] = atm["longitude"] = None
                atm["location_source"] = None
        cassettes = conn.execute(
            "SELECT * FROM cassette_snapshots WHERE atm_snapshot_id = %s ORDER BY cassette_index", (snapshot["id"],)
        ).fetchall() if snapshot else []
        cycles = rows("SELECT * FROM btech_encashments WHERE atm_id = %s ORDER BY started_at DESC")
        ids = [c["id"] for c in cycles]
        cycle_cassettes: dict = {}
        for c in conn.execute(
            "SELECT * FROM btech_encashment_cassettes WHERE encashment_id = ANY(%s) ORDER BY number", (ids,)
        ).fetchall():
            cycle_cassettes.setdefault(c["encashment_id"], []).append(c)
        cycle_actions: dict = {}
        for a in conn.execute(
            "SELECT * FROM btech_encashment_actions WHERE encashment_id = ANY(%s) ORDER BY created_at, id", (ids,)
        ).fetchall():
            cycle_actions.setdefault(a["encashment_id"], []).append(a)
        for c in cycles:
            c["cassettes"] = cycle_cassettes.get(c["id"], [])
            c["actions"] = cycle_actions.get(c["id"], [])
        load_days = {
            r["day"] for r in conn.execute(
                "SELECT (started_at AT TIME ZONE %s)::date::text AS day FROM btech_encashments WHERE atm_id = %s",
                (settings.TIMEZONE, atm_id),
            ).fetchall()
        }
        history = daily_series(
            rows("SELECT * FROM btech_balance_daily WHERE atm_id = %s AND day >= current_date - 92"),
            rows("SELECT * FROM btech_turnover_daily WHERE atm_id = %s AND day >= current_date - 92"),
            load_days,
        )
        same_tid = conn.execute(
            "SELECT id, address, last_seen_at FROM atms WHERE tid = %s AND id <> %s ORDER BY last_seen_at DESC NULLS LAST",
            (atm["tid"], atm_id),
        ).fetchall()
        return {
            "atm": atm,
            "same_tid": same_tid,
            "history": history,
            "snapshot": snapshot,
            "cassettes": cassettes,
            "turnover": one("SELECT * FROM turnover_snapshots WHERE atm_id = %s ORDER BY polled_at DESC, id DESC LIMIT 1"),
            "sync": one("SELECT * FROM btech_encashment_sync WHERE atm_id = %s"),
            "encashments": cycles,
            "events": rows("SELECT at, kind, old_value, new_value FROM atm_state_events WHERE atm_id = %s ORDER BY at DESC, id DESC LIMIT 200"),
            "daily": rows("SELECT * FROM atm_daily WHERE atm_id = %s ORDER BY day DESC LIMIT 92"),
            "recommendation": one("SELECT * FROM recommendations WHERE atm_id = %s ORDER BY created_at DESC, id DESC LIMIT 1"),
        }


@router.post("/history/process", summary="Обработать накопленные снимки (события, инкассации, итоги)")
def history_process():
    from .history import process_history

    return process_history()
