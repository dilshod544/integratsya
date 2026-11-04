"""API страницы «Менеджер банкоматов» (dashboard/atm-monitor.html): текущее
состояние каждого банкомата из БД сборщика (последний снимок) + ручной запуск опроса.
Подключается в api/main.py как router."""
import threading

from fastapi import APIRouter

from .db import connect
from .scheduler import run_once

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


@router.post("/history/process", summary="Обработать накопленные снимки (события, инкассации, итоги)")
def history_process():
    from .history import process_history

    return process_history()
