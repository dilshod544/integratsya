"""Прогноз снятий наличных по банкоматам (XGBoost) на данных сборщика BTech.

Источник — почасовые снимки остатка (atm_snapshots.cdm_total_uzs). Из них
получаются суточные снятия по каждому ATM: сумма падений остатка между
соседними опросами. Рост остатка — инкассация или депозиты, а не снятие;
падение почти в ноль с возвратом остатка на следующем опросе — замена кассет,
тоже не снятие.

Модель — XGBRegressor (objective tweedie: неотрицательные суммы, бывают нули)
на лагах, скользящих средних и календаре (день недели, дни зарплаты,
праздники). Прогноз строится рекурсивно на FORECAST_DAYS дней вперёд; по нему
считается, когда остаток опустится ниже LOW_CASH_PCT ёмкости и до нуля, и
сколько загрузить, чтобы хватило на весь горизонт. Остаток для проекции —
доступный (без неисправных кассет, см. history.py), ёмкость — максимум,
который реально загружали в этот банкомат (history.capacity_by_atm).

Пока истории мало (первые недели после запуска сборщика), XGBoost не
обучается и работает запасной вариант: среднее за последние полные дни или
скорость расхода от самой BTech (turnover forecast). Что сработало, видно в
forecast_runs.model и atm_forecast_summary.method.

Переобучение — раз в сутки в FORECAST_HOUR:00 по TIMEZONE (start_background),
вручную — run_forecast() или POST /api/analytics/forecast/run."""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .config import settings
from .db import connect

logger = logging.getLogger("atm_monitor.forecast")

MAX_GAP_HOURS = 3          # пара опросов дальше друг от друга — не считаем (пропуск данных)
MIN_DAY_COVERAGE_H = 12    # день с меньшим покрытием опросами не идёт в обучение
MIN_TRAIN_ROWS = 200       # меньше строк ATM×день — XGBoost не обучаем
MIN_TRAIN_DAYS = 14
MIN_ATM_DAYS = 3           # ATM с меньшей историей прогнозируется запасным способом
HOLDOUT_DAYS = 7           # последние дни истории — для оценки качества модели
RETRY_DELAY_SEC = 30 * 60
STALE_BALANCE_HOURS = 48   # остаток старше — прогноз от него бессмыслен, ATM пропускаем
# Больше в банкомат физически не помещается (≈5 кассет × 2500 купюр × 200 000);
# такие остатки BTech отдаёт у тестовых/неверно настроенных устройств.
MAX_PLAUSIBLE_UZS = 3_000_000_000
KEEP_RUNS_DAYS = 30

SALARY_DAYS = (10, 25)
# Фиксированные выходные Узбекистана (Хайиты по лунному календарю сюда не входят).
HOLIDAYS = {(1, 1), (3, 8), (3, 21), (5, 9), (9, 1), (10, 1), (12, 8)}

LAGS = (1, 2, 3, 4, 5, 6, 7, 14)
FEATURES = [
    *(f"lag_{k}" for k in LAGS),
    "mean_7", "mean_14", "mean_28", "std_7", "min_7", "max_7", "atm_mean",
    "dow", "dom", "month", "is_weekend", "salary_dist", "is_holiday", "days_left_in_month",
]

XGB_PARAMS = {
    "n_estimators": 300,
    "learning_rate": 0.05,
    "max_depth": 5,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 3,
    "objective": "reg:tweedie",
    "tweedie_variance_power": 1.3,
    "tree_method": "hist",
    "random_state": 42,
    "n_jobs": 2,   # обучение идёт в фоне рядом с сервером — не забираем все ядра
}

# Снятия = сумма падений остатка между соседними опросами; {bucket} — сутки или час.
_WITHDRAWALS_SQL = """
WITH s AS (
    SELECT atm_id, polled_at, cdm_total_uzs AS bal,
           LAG(cdm_total_uzs)  OVER w AS prev_bal,
           LAG(polled_at)      OVER w AS prev_at,
           LEAD(cdm_total_uzs) OVER w AS next_bal
    FROM atm_snapshots
    WHERE polled_at >= %(since)s AND cdm_total_uzs IS NOT NULL AND cdm_total_uzs <= %(max_bal)s
    WINDOW w AS (PARTITION BY atm_id ORDER BY polled_at, id)
), p AS (
    SELECT atm_id, polled_at, bal, prev_bal, next_bal,
           EXTRACT(EPOCH FROM polled_at - prev_at) / 3600.0 AS gap_h
    FROM s
    WHERE prev_at IS NOT NULL
)
SELECT atm_id,
       {bucket} AS day,
       SUM(CASE WHEN prev_bal > bal
                 AND NOT (bal <= prev_bal * 0.1 AND COALESCE(next_bal, 0) >= prev_bal * 0.5)
                THEN prev_bal - bal ELSE 0 END) AS withdrawn,
       SUM(gap_h) AS covered_hours,
       COUNT(*) FILTER (WHERE bal - prev_bal > GREATEST(prev_bal * 0.05, 1000000)) AS refills
FROM p
WHERE gap_h <= %(max_gap)s
GROUP BY 1, 2
ORDER BY 1, 2
"""
_DAILY_SQL = _WITHDRAWALS_SQL.replace("{bucket}", "(polled_at AT TIME ZONE %(tz)s)::date")
_HOURLY_SQL = _WITHDRAWALS_SQL.replace("{bucket}", "date_trunc('hour', polled_at AT TIME ZONE %(tz)s)")

_CURRENT_SQL = """
SELECT a.id AS atm_id, COALESCE(s.cdm_available_uzs, s.cdm_total_uzs) AS balance, s.polled_at,
       (SELECT t.forecast_hours FROM turnover_snapshots t
         WHERE t.atm_id = a.id AND (t.currency IS NULL OR UPPER(t.currency) = 'UZS')
         ORDER BY t.polled_at DESC, t.id DESC LIMIT 1) AS btech_hours
FROM atms a
JOIN atm_snapshots s ON s.id = (
    SELECT id FROM atm_snapshots WHERE atm_id = a.id ORDER BY polled_at DESC, id DESC LIMIT 1
)
WHERE s.cdm_total_uzs IS NOT NULL AND s.cdm_total_uzs <= %(max_bal)s AND s.polled_at >= %(fresh_since)s
"""


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.TIMEZONE)


# ── Данные ───────────────────────────────────────────────────

def daily_withdrawals(conn, days: int) -> pd.DataFrame:
    """Суточные снятия по каждому ATM за последние `days` дней (по местной дате).

    Колонки: atm_id, day (Timestamp), withdrawn (UZS, как наблюдалось),
    covered_hours (сколько часов суток покрыто опросами), refills."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        _DAILY_SQL,
        {"since": since, "tz": settings.TIMEZONE, "max_gap": MAX_GAP_HOURS, "max_bal": MAX_PLAUSIBLE_UZS},
    ).fetchall()
    df = pd.DataFrame(rows, columns=["atm_id", "day", "withdrawn", "covered_hours", "refills"])
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"])
    df["withdrawn"] = df["withdrawn"].astype(float)
    df["covered_hours"] = df["covered_hours"].astype(float)
    df["refills"] = df["refills"].astype(int)
    return df


def hourly_withdrawals(conn, days: int) -> pd.DataFrame:
    """То же, что daily_withdrawals, но по часам (местное время): колонка hour вместо day."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        _HOURLY_SQL,
        {"since": since, "tz": settings.TIMEZONE, "max_gap": MAX_GAP_HOURS, "max_bal": MAX_PLAUSIBLE_UZS},
    ).fetchall()
    df = pd.DataFrame(rows, columns=["atm_id", "day", "withdrawn", "covered_hours", "refills"])
    df = df.rename(columns={"day": "hour"})
    if df.empty:
        return df
    df["hour"] = pd.to_datetime(df["hour"])
    df["withdrawn"] = df["withdrawn"].astype(float)
    df["covered_hours"] = df["covered_hours"].astype(float)
    df["refills"] = df["refills"].astype(int)
    return df


def _complete_days(daily: pd.DataFrame, today: pd.Timestamp) -> pd.DataFrame:
    """Только завершённые дни с достаточным покрытием; снятия приведены к 24 часам."""
    d = daily[(daily["day"] < today) & (daily["covered_hours"] >= MIN_DAY_COVERAGE_H)].copy()
    d["withdrawal"] = d["withdrawn"] * 24.0 / d["covered_hours"].clip(lower=1.0, upper=24.0)
    return d[["atm_id", "day", "withdrawal"]]


# ── Признаки ─────────────────────────────────────────────────

def _calendar(frame: pd.DataFrame) -> None:
    day = frame["day"]
    frame["dow"] = day.dt.dayofweek
    frame["dom"] = day.dt.day
    frame["month"] = day.dt.month
    frame["is_weekend"] = (frame["dow"] >= 5).astype(int)
    frame["days_left_in_month"] = day.dt.days_in_month - frame["dom"]
    # Расстояние до ближайшего дня зарплаты (с учётом соседнего месяца).
    dom = frame["dom"]
    dist = pd.concat([(dom - s).abs() for s in SALARY_DAYS], axis=1).min(axis=1)
    frame["salary_dist"] = dist.clip(upper=15)
    frame["is_holiday"] = [int((m, d) in HOLIDAYS) for m, d in zip(frame["month"], dom)]


def _features(frame: pd.DataFrame) -> pd.DataFrame:
    """frame: atm_id, day, withdrawal (NaN — неизвестно), отсортирован по atm_id, day,
    без пропусков дат. Признаки строки используют только предыдущие дни."""
    out = frame[["atm_id", "day", "withdrawal"]].copy()
    g = out.groupby("atm_id", sort=False)["withdrawal"]
    for k in LAGS:
        out[f"lag_{k}"] = g.shift(k)
    prev = g.shift(1)
    pg = prev.groupby(out["atm_id"], sort=False)
    for w in (7, 14, 28):
        out[f"mean_{w}"] = pg.transform(lambda s, w=w: s.rolling(w, min_periods=3).mean())
    out["std_7"] = pg.transform(lambda s: s.rolling(7, min_periods=3).std())
    out["min_7"] = pg.transform(lambda s: s.rolling(7, min_periods=3).min())
    out["max_7"] = pg.transform(lambda s: s.rolling(7, min_periods=3).max())
    out["atm_mean"] = pg.transform(lambda s: s.expanding(min_periods=1).mean())
    _calendar(out)
    return out


def _dense(hist: pd.DataFrame, last_day: pd.Timestamp) -> pd.DataFrame:
    """Сетка ATM × каждый день от первой истории ATM до last_day включительно."""
    parts = []
    for atm_id, grp in hist.groupby("atm_id", sort=True):
        days = pd.date_range(grp["day"].min(), last_day, freq="D")
        s = grp.set_index("day")["withdrawal"].reindex(days)
        parts.append(pd.DataFrame({"atm_id": atm_id, "day": days, "withdrawal": s.values}))
    if not parts:
        return pd.DataFrame(columns=["atm_id", "day", "withdrawal"])
    return pd.concat(parts, ignore_index=True)


# ── Модель ───────────────────────────────────────────────────

def _fit(train: pd.DataFrame):
    import xgboost as xgb  # тяжёлый импорт — только когда реально обучаем

    model = xgb.XGBRegressor(**XGB_PARAMS)
    model.fit(train[FEATURES].astype(float), train["withdrawal"].astype(float))
    return model


def _wape(actual: pd.Series, pred: pd.Series) -> Optional[float]:
    total = float(actual.abs().sum())
    return round(float((actual - pred).abs().sum()) / total, 4) if total else None


def train_model(hist: pd.DataFrame, today: pd.Timestamp):
    """Обучает XGBoost на полных днях истории. Возвращает (model | None, metrics, train_rows).

    model=None, если истории пока мало — тогда вызывающий берёт запасной метод."""
    feats = _features(_dense(hist, today - pd.Timedelta(days=1)))
    rows = feats[feats["withdrawal"].notna() & feats["mean_7"].notna()]
    metrics: Dict[str, Any] = {"train_rows": int(len(rows)), "train_days": int(rows["day"].nunique())}
    if len(rows) < MIN_TRAIN_ROWS or rows["day"].nunique() < MIN_TRAIN_DAYS:
        metrics["reason"] = (
            f"мало истории: {len(rows)} строк ATM×день за {rows['day'].nunique()} дн. "
            f"(нужно ≥{MIN_TRAIN_ROWS} строк и ≥{MIN_TRAIN_DAYS} дн.)"
        )
        return None, metrics, len(rows)

    # Честная оценка: учим без последних HOLDOUT_DAYS дней и сравниваем с простым
    # средним за 7 дней — так видно, что модель действительно лучше «наивного» прогноза.
    cutoff = today - pd.Timedelta(days=HOLDOUT_DAYS)
    fit_part, holdout = rows[rows["day"] < cutoff], rows[rows["day"] >= cutoff]
    if len(fit_part) >= MIN_TRAIN_ROWS and len(holdout):
        pred = pd.Series(_fit(fit_part).predict(holdout[FEATURES].astype(float)), index=holdout.index)
        metrics.update(
            holdout_days=HOLDOUT_DAYS,
            holdout_rows=int(len(holdout)),
            mae=round(float((holdout["withdrawal"] - pred).abs().mean())),
            wape=_wape(holdout["withdrawal"], pred),
            baseline_mae=round(float((holdout["withdrawal"] - holdout["mean_7"]).abs().mean())),
            baseline_wape=_wape(holdout["withdrawal"], holdout["mean_7"]),
        )

    return _fit(rows), metrics, len(rows)


def predict_days(model, hist: pd.DataFrame, today: pd.Timestamp, horizon: int) -> Dict[int, List[float]]:
    """Рекурсивный прогноз: день t+1 использует предсказание дня t как лаг."""
    last = today + pd.Timedelta(days=horizon - 1)
    frame = _dense(hist, last)
    future = frame["day"] >= today
    frame.loc[future, "withdrawal"] = float("nan")
    for step in range(horizon):
        d = today + pd.Timedelta(days=step)
        feats = _features(frame)
        mask = feats["day"] == d
        pred = model.predict(feats.loc[mask, FEATURES].astype(float))
        frame.loc[mask.values, "withdrawal"] = [max(0.0, float(p)) for p in pred]
    out = frame[future].sort_values(["atm_id", "day"])
    return {int(a): grp["withdrawal"].tolist() for a, grp in out.groupby("atm_id")}


# ── Проекция остатка ─────────────────────────────────────────

def project_balance(
    balance: float,
    daily: List[float],
    now: datetime,
    first_day: date,
    low_level: float,
    capacity: float,
) -> Dict[str, Any]:
    """Расход внутри суток считаем равномерным. Возвращает момент, когда остаток
    станет ниже low_level и нуля (None — не в пределах горизонта), остаток на
    конец каждого дня, снятия в ближайшие 24 часа и рекомендуемую загрузку."""
    tz = now.tzinfo
    bal = float(balance)
    low_at = bal_zero_at = None
    if bal <= low_level:
        low_at = now
    if bal <= 0:
        bal_zero_at = now
    horizon_end = now + timedelta(hours=24)
    next_24h = demand = 0.0
    ends = []
    for i, amount in enumerate(daily):
        day_start = datetime.combine(first_day + timedelta(days=i), datetime.min.time(), tz)
        day_end = day_start + timedelta(days=1)
        start = max(day_start, now)
        rate = amount / 24.0  # UZS в час
        hours = max(0.0, (day_end - start).total_seconds() / 3600)
        spend = rate * hours
        demand += spend
        overlap = (min(day_end, horizon_end) - start).total_seconds() / 3600
        next_24h += rate * max(0.0, overlap)
        if rate > 0:
            if low_at is None and bal - spend <= low_level:
                low_at = start + timedelta(hours=(bal - low_level) / rate)
            if bal_zero_at is None and bal - spend <= 0:
                bal_zero_at = start + timedelta(hours=bal / rate)
        bal = max(0.0, bal - spend)
        ends.append(bal)
    # Загрузить столько, чтобы покрыть спрос до конца горизонта и не опуститься ниже
    # порога; больше ёмкости кассет загрузить нельзя.
    refill = max(0.0, demand + low_level - float(balance))
    refill = min(refill, max(0.0, capacity - float(balance)))
    return {
        "low_cash_at": low_at,
        "cashout_at": bal_zero_at,
        "balance_ends": ends,
        "withdrawal_24h": next_24h,
        "withdrawal_horizon": demand,
        "refill_uzs": math.ceil(refill / 1_000_000) * 1_000_000,
    }


# ── Прогон ───────────────────────────────────────────────────

def _as_utc(value) -> datetime:
    ts = pd.Timestamp(value)
    return (ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")).to_pydatetime()


def build_forecast(conn, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Считает прогноз для всех ATM (без записи в БД)."""
    tz = _tz()
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = pd.Timestamp(now.date())
    horizon = settings.FORECAST_DAYS

    daily = daily_withdrawals(conn, settings.FORECAST_HISTORY_DAYS)
    hist = _complete_days(daily, today) if not daily.empty else pd.DataFrame(columns=["atm_id", "day", "withdrawal"])
    days_by_atm = hist.groupby("atm_id")["day"].nunique().to_dict() if not hist.empty else {}

    if daily.empty:
        reason = "нет истории снимков"
    else:
        reason = f"нет ни одного полного дня (нужно ≥{MIN_DAY_COVERAGE_H} ч опросов в сутки)"
    model, metrics, train_rows = (None, {"reason": reason}, 0)
    if not hist.empty:
        try:
            model, metrics, train_rows = train_model(hist, today)
        except Exception as e:  # noqa: BLE001 — нет xgboost/libomp и т.п.: работаем запасным методом
            logger.exception("XGBoost не обучился, используем запасной метод")
            metrics = {"reason": f"ошибка обучения: {e}"}

    model_preds: Dict[int, List[float]] = {}
    if model is not None:
        # Модель — только для ATM со свежей историей: у давно замолчавшего лаги пустые.
        fresh = hist[hist["day"] >= today - pd.Timedelta(days=28)]
        fresh_days = fresh.groupby("atm_id")["day"].nunique()
        eligible = fresh_days[fresh_days >= MIN_ATM_DAYS].index.tolist()
        if eligible:
            model_preds = predict_days(model, hist[hist["atm_id"].isin(eligible)], today, horizon)

    recent = hist[hist["day"] >= today - pd.Timedelta(days=7)] if not hist.empty else hist
    mean_recent = recent.groupby("atm_id")["withdrawal"].mean().to_dict() if not recent.empty else {}

    from .history import capacity_by_atm

    capacities = capacity_by_atm(conn, settings.DEFAULT_CAPACITY)
    results = []
    current = conn.execute(
        _CURRENT_SQL,
        {"max_bal": MAX_PLAUSIBLE_UZS, "fresh_since": now - timedelta(hours=STALE_BALANCE_HOURS)},
    ).fetchall()
    for cur in current:
        atm_id = int(cur["atm_id"])
        balance = float(cur["balance"])
        if atm_id in model_preds:
            method, preds = "xgboost", model_preds[atm_id]
        elif atm_id in mean_recent:
            method, preds = "mean_7d", [float(mean_recent[atm_id])] * horizon
        elif cur.get("btech_hours") and float(cur["btech_hours"]) > 0 and balance > 0:
            method, preds = "btech", [balance / float(cur["btech_hours"]) * 24.0] * horizon
        else:
            continue
        capacity = capacities.get(atm_id, float(settings.DEFAULT_CAPACITY))
        low_level = settings.LOW_CASH_PCT * capacity
        proj = project_balance(balance, preds, now, now.date(), low_level, capacity)
        results.append({
            "atm_id": atm_id,
            "method": method,
            "history_days": int(days_by_atm.get(atm_id, 0)),
            "balance_uzs": balance,
            "capacity_uzs": capacity,
            "daily": preds,
            **proj,
        })

    model_name = "xgboost" if model is not None else ("fallback" if results else "none")
    return {
        "now": now,
        "first_day": now.date(),
        "horizon": horizon,
        "model": model_name,
        "metrics": metrics,
        "train_rows": train_rows,
        "results": results,
    }


def _save(conn, run_id: int, fc: Dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO atm_forecast_summary (run_id, atm_id, method, history_days, balance_uzs,
                withdrawal_24h, withdrawal_horizon, low_cash_at, cashout_at, refill_uzs)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                (run_id, r["atm_id"], r["method"], r["history_days"], r["balance_uzs"],
                 r["withdrawal_24h"], r["withdrawal_horizon"], r["low_cash_at"], r["cashout_at"],
                 r["refill_uzs"])
                for r in fc["results"]
            ],
        )
        cur.executemany(
            """
            INSERT INTO atm_forecasts (run_id, atm_id, forecast_date, withdrawal_uzs, balance_end_uzs)
            VALUES (%s,%s,%s,%s,%s)
            """,
            [
                (run_id, r["atm_id"], fc["first_day"] + timedelta(days=i), amount, r["balance_ends"][i])
                for r in fc["results"]
                for i, amount in enumerate(r["daily"])
            ],
        )


_run_lock = threading.Lock()


def run_forecast() -> Optional[int]:
    """Переобучает модель и сохраняет прогноз. Возвращает число ATM с прогнозом
    (None — ошибка или прогон уже идёт). Ошибки не пробрасываются."""
    if not _run_lock.acquire(blocking=False):
        logger.info("Прогноз уже считается — пропускаем.")
        return None
    started = datetime.now(timezone.utc)
    run_id = None
    from .history import safe_process_history

    safe_process_history()   # доступный остаток и ёмкость нужны прогнозу
    try:
        with connect() as conn:
            run_id = conn.execute(
                "INSERT INTO forecast_runs (started_at, status, horizon_days) VALUES (%s, 'running', %s) RETURNING id",
                (started, settings.FORECAST_DAYS),
            ).fetchone()["id"]
        t0 = time.time()
        with connect() as conn:
            fc = build_forecast(conn)
            _save(conn, run_id, fc)
            fc["metrics"]["seconds"] = round(time.time() - t0, 1)
            conn.execute(
                """
                UPDATE forecast_runs SET status='success', finished_at=%s, model=%s,
                       train_rows=%s, atm_count=%s, metrics=%s WHERE id=%s
                """,
                (datetime.now(timezone.utc), fc["model"], fc["train_rows"], len(fc["results"]),
                 json.dumps(fc["metrics"], ensure_ascii=False), run_id),
            )
            conn.execute(
                "DELETE FROM forecast_runs WHERE started_at < %s",
                (started - timedelta(days=KEEP_RUNS_DAYS),),
            )
        logger.info(
            "Прогноз снятий готов: модель=%s, ATM=%d, строк обучения=%d, %s",
            fc["model"], len(fc["results"]), fc["train_rows"], fc["metrics"],
        )
        return len(fc["results"])
    except Exception as e:  # noqa: BLE001
        logger.exception("Прогноз снятий завершился с ошибкой")
        if run_id is not None:
            try:
                with connect() as conn:
                    conn.execute(
                        "UPDATE forecast_runs SET status='failed', finished_at=%s, error_message=%s WHERE id=%s",
                        (datetime.now(timezone.utc), str(e), run_id),
                    )
            except Exception:  # noqa: BLE001
                pass
        return None
    finally:
        _run_lock.release()


# ── Расписание ───────────────────────────────────────────────

def is_due(now_local: datetime, last_run_local: Optional[datetime], hour: int) -> bool:
    """Пора ли переобучать: последний успешный прогон был до последнего наступившего HH:00."""
    slot = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now_local < slot:
        slot -= timedelta(days=1)
    return last_run_local is None or last_run_local < slot


def _last_success_local() -> Optional[datetime]:
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT MAX(started_at) AS ts FROM forecast_runs WHERE status='success' AND atm_count > 0"
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return _as_utc(row["ts"]).astimezone(_tz()) if row and row["ts"] else None


_thread = None
_stop = threading.Event()


def _loop():
    # Как и сборщик, просыпаемся раз в минуту и сверяемся с настенными часами:
    # пропущенный из-за сна машины ночной прогон выполнится сразу после пробуждения.
    retry_at = 0.0
    while not _stop.is_set():
        if time.time() >= retry_at and is_due(datetime.now(_tz()), _last_success_local(), settings.FORECAST_HOUR):
            count = run_forecast()
            # Нет данных или ошибка — повторяем не каждую минуту, а через RETRY_DELAY_SEC.
            retry_at = time.time() + RETRY_DELAY_SEC if not count else 0.0
        _stop.wait(60)


def start_background():
    global _thread
    if not settings.FORECAST_ENABLED:
        logger.info("Прогноз снятий отключён (ATM_FORECAST=0).")
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="atm-forecast", daemon=True)
    _thread.start()
    logger.info("Прогноз снятий: переобучение ежедневно в %02d:00 (%s).", settings.FORECAST_HOUR, settings.TIMEZONE)


def stop_background():
    _stop.set()
