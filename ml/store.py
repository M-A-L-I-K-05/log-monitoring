"""Запись результатов ML в PostgreSQL (витрина для Grafana).

Таблицы создаёт сам сервис (CREATE TABLE IF NOT EXISTS) — init.sql не трогаем,
он всё равно не перезапускается на существующем volume.
"""
import json
import logging
from datetime import datetime

from psycopg_pool import ConnectionPool

import config

logger = logging.getLogger("ml.store")

_pool: ConnectionPool | None = None


DDL = """
CREATE TABLE IF NOT EXISTS ml_runs (
    run_id        BIGSERIAL   PRIMARY KEY,
    kind          TEXT        NOT NULL,            -- run|detect|forecast|train
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_from   TIMESTAMP,                       -- виртуальное время начала окна
    window_to     TIMESTAMP,
    n_machines    INTEGER     NOT NULL DEFAULT 0,
    n_points      INTEGER     NOT NULL DEFAULT 0,
    n_anomalies   INTEGER     NOT NULL DEFAULT 0,
    n_forecasts   INTEGER     NOT NULL DEFAULT 0,
    params        JSONB
);

CREATE TABLE IF NOT EXISTS ml_anomalies (
    id            BIGSERIAL   PRIMARY KEY,
    run_id        BIGINT      REFERENCES ml_runs(run_id) ON DELETE CASCADE,
    machine_id    TEXT        NOT NULL,
    machine_type  TEXT,
    product_code  TEXT,
    event_time    TIMESTAMP   NOT NULL,
    score_ecod    REAL,
    score_iforest REAL,
    is_anomaly    BOOLEAN     NOT NULL,
    top_sensor    TEXT,
    top_sensor_z  REAL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ml_anomalies_machine_idx ON ml_anomalies (machine_id);
CREATE INDEX IF NOT EXISTS ml_anomalies_time_idx    ON ml_anomalies (event_time);
CREATE INDEX IF NOT EXISTS ml_anomalies_flag_idx    ON ml_anomalies (is_anomaly);
CREATE INDEX IF NOT EXISTS ml_anomalies_type_idx    ON ml_anomalies (machine_type, product_code);

CREATE TABLE IF NOT EXISTS ml_forecasts (
    id            BIGSERIAL   PRIMARY KEY,
    run_id        BIGINT      REFERENCES ml_runs(run_id) ON DELETE CASCADE,
    machine_id    TEXT        NOT NULL,
    sensor        TEXT        NOT NULL,
    ts            TIMESTAMP   NOT NULL,            -- виртуальное время (история+горизонт)
    yhat          REAL,
    yhat_lower    REAL,
    yhat_upper    REAL,
    actual        REAL,
    breach        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ml_forecasts_machine_idx ON ml_forecasts (machine_id, sensor);
CREATE INDEX IF NOT EXISTS ml_forecasts_time_idx    ON ml_forecasts (ts);

-- Витрина статуса Prophet для карточек Grafana: одна строка на (станок, сенсор),
-- перезаписывается каждым прогнозным циклом. is_anomaly=true → прогноз выходит за
-- норму сенсора в пределах горизонта (предиктивный сигнал).
CREATE TABLE IF NOT EXISTS ml_prophet_status (
    machine_id       TEXT        NOT NULL,
    machine_type     TEXT,
    context          TEXT,                          -- тип шестерни или фаза печи
    sensor           TEXT        NOT NULL,
    is_anomaly       BOOLEAN     NOT NULL DEFAULT FALSE,
    n_breaches       INTEGER     NOT NULL DEFAULT 0, -- точек прогноза за нормой
    horizon_points   INTEGER     NOT NULL DEFAULT 0, -- всего точек на горизонте
    lead_min         REAL,                           -- через сколько мин первый выход
    norm_lower       REAL,
    norm_upper       REAL,
    yhat_end         REAL,                           -- прогноз на конце горизонта
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (machine_id, sensor)
);
CREATE INDEX IF NOT EXISTS ml_prophet_status_machine_idx ON ml_prophet_status (machine_id);
CREATE INDEX IF NOT EXISTS ml_prophet_status_flag_idx    ON ml_prophet_status (is_anomaly);
"""


def init(database_url: str = None) -> None:
    global _pool
    _pool = ConnectionPool(database_url or config.DATABASE_URL,
                           min_size=1, max_size=5, kwargs={"connect_timeout": 3})
    _pool.wait()
    ensure_tables()


def close() -> None:
    if _pool is not None:
        _pool.close()


def ensure_tables() -> None:
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            # Миграция старых БД: колонки должны существовать до CREATE INDEX по ним.
            # IF EXISTS — на свежей БД таблицы ещё нет, это безопасный no-op (иначе
            # ALTER упал бы с undefined_table и завалил старт). IF NOT EXISTS — на
            # уже мигрированной БД повторный запуск тоже no-op.
            cur.execute(
                "ALTER TABLE IF EXISTS ml_anomalies "
                "ADD COLUMN IF NOT EXISTS machine_type TEXT")
            cur.execute(
                "ALTER TABLE IF EXISTS ml_anomalies "
                "ADD COLUMN IF NOT EXISTS product_code TEXT")
            # Затем DDL (CREATE TABLE IF NOT EXISTS + индексы)
            cur.execute(DDL)
    logger.info("ml_tables_ready")


def new_run(kind: str, window_from: datetime | None, window_to: datetime | None,
            params: dict | None = None) -> int:
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ml_runs (kind, window_from, window_to, params) "
                "VALUES (%s, %s, %s, %s) RETURNING run_id",
                (kind, window_from, window_to,
                 json.dumps(params or {})),
            )
            return cur.fetchone()[0]


def finalize_run(run_id: int, n_machines: int, n_points: int,
                 n_anomalies: int, n_forecasts: int) -> None:
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ml_runs SET n_machines=%s, n_points=%s, "
                "n_anomalies=%s, n_forecasts=%s WHERE run_id=%s",
                (n_machines, n_points, n_anomalies, n_forecasts, run_id),
            )


def insert_anomalies(run_id: int, machine_id: str, scored,
                     machine_type: str = None, product_code: str = None) -> int:
    """scored — DataFrame из MachineDetector.score (index = event_time)."""
    if scored is None or scored.empty:
        return 0
    rows = [
        (run_id, machine_id, machine_type, product_code, idx.to_pydatetime(),
         float(r.score_ecod), float(r.score_iforest), bool(r.is_anomaly),
         str(r.top_sensor), float(r.top_sensor_z))
        for idx, r in scored.iterrows()
    ]
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ml_anomalies (run_id, machine_id, machine_type, "
                "product_code, event_time, score_ecod, score_iforest, "
                "is_anomaly, top_sensor, top_sensor_z) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
    return len(rows)


def insert_forecasts(run_id: int, machine_id: str, sensor: str, fc) -> int:
    """fc — DataFrame из forecaster.forecast_series (колонки ts/yhat/.../breach)."""
    if fc is None or fc.empty:
        return 0
    rows = [
        (run_id, machine_id, sensor, row.ts.to_pydatetime(),
         float(row.yhat), float(row.yhat_lower), float(row.yhat_upper),
         (None if pd_isna(row.actual) else float(row.actual)),
         bool(row.breach))
        for row in fc.itertuples(index=False)
    ]
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            # Заменяем предыдущий прогноз этого ряда — в витрине нужен последний,
            # а не накопление перекрывающихся ts от каждого прогона.
            # Печь в любой момент в одной фазе, поэтому ключа (machine_id, sensor)
            # достаточно даже для общего сенсора фаз (furnace_temp_zone1).
            cur.execute(
                "DELETE FROM ml_forecasts WHERE machine_id=%s AND sensor=%s",
                (machine_id, sensor),
            )
            cur.executemany(
                "INSERT INTO ml_forecasts (run_id, machine_id, sensor, ts, "
                "yhat, yhat_lower, yhat_upper, actual, breach) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
    return len(rows)


def upsert_prophet_status(rows: list[tuple]) -> int:
    """Перезаписывает статус Prophet по (machine_id, sensor).

    rows — кортежи (machine_id, machine_type, context, sensor, is_anomaly,
    n_breaches, horizon_points, lead_min, norm_lower, norm_upper, yhat_end).
    """
    if not rows:
        return 0
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ml_prophet_status (machine_id, machine_type, context, "
                "sensor, is_anomaly, n_breaches, horizon_points, lead_min, "
                "norm_lower, norm_upper, yhat_end, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
                "ON CONFLICT (machine_id, sensor) DO UPDATE SET "
                "machine_type=EXCLUDED.machine_type, context=EXCLUDED.context, "
                "is_anomaly=EXCLUDED.is_anomaly, n_breaches=EXCLUDED.n_breaches, "
                "horizon_points=EXCLUDED.horizon_points, lead_min=EXCLUDED.lead_min, "
                "norm_lower=EXCLUDED.norm_lower, norm_upper=EXCLUDED.norm_upper, "
                "yhat_end=EXCLUDED.yhat_end, updated_at=now()",
                rows,
            )
    return len(rows)


def prune_prophet_status(max_age_sec: float) -> int:
    """Удаляет строки витрины Prophet, не обновлявшиеся дольше max_age_sec.

    Так карточка станка, который встал или сменил фазу (и больше не пишет этот
    сенсор), перестаёт «застревать» с прошлым статусом.
    """
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ml_prophet_status "
                "WHERE updated_at < now() - make_interval(secs => %s)",
                (max_age_sec,),
            )
            return cur.rowcount


def truncate_all() -> None:
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ml_anomalies, ml_forecasts, ml_runs RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ml_prophet_status")
    logger.info("ml_tables_truncated")


def pd_isna(v) -> bool:
    try:
        import math
        return v is None or (isinstance(v, float) and math.isnan(v))
    except Exception:
        return v is None
