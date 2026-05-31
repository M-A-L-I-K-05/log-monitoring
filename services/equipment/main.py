import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from psycopg_pool import ConnectionPool
from pythonjsonlogger import jsonlogger
from fastapi import FastAPI
from pydantic import BaseModel


def setup_logging(service_name: str) -> logging.Logger:
    formatter = jsonlogger.JsonFormatter(
        "%(levelname)s %(name)s %(message)s",
        rename_fields={
            "levelname": "level",
            "name": "service",
            "message": "event",
        },
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = False
        uv_logger.disabled = True
    return logging.getLogger(service_name)


logger = setup_logging("equipment")

DATABASE_URL = os.environ.get("DATABASE_URL")
pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global pool
    pool = ConnectionPool(DATABASE_URL, min_size=2, max_size=10, kwargs={"connect_timeout": 2})
    pool.wait()
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Equipment Service", lifespan=lifespan)


def db_update_machine_state(machine_id: str, new_state: str, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE machine_status SET current_state = %s, state_changed_at = %s WHERE machine_id = %s",
                    (new_state, event_time, machine_id),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": machine_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "update_machine_state",
                    "error": str(exc),
                },
            },
        )
        return False


def db_update_sensor_timestamp(machine_id: str, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE machine_status SET sensor_updated_at = %s WHERE machine_id = %s",
                    (event_time, machine_id),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": machine_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "update_sensor_timestamp",
                    "error": str(exc),
                },
            },
        )
        return False


def db_update_sensor_timestamps_batch(latest_by_machine: dict[str, datetime]) -> bool:
    """Один транзакционный батч: по одному UPDATE на каждую машину с её MAX(event_time)."""
    if not latest_by_machine:
        return True
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE machine_status SET sensor_updated_at = %s WHERE machine_id = %s",
                    [(event_time, machine_id) for machine_id, event_time in latest_by_machine.items()],
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "details": {
                    "operation": "update_sensor_timestamps_batch",
                    "machines": list(latest_by_machine.keys()),
                    "error": str(exc),
                },
            },
        )
        return False


class SensorReading(BaseModel):
    machine_id: str
    machine_type: str
    work_center: str
    product_code: str | None = None
    readings: dict[str, float]
    event_time: datetime


class StateChange(BaseModel):
    machine_id: str
    machine_type: str
    work_center: str
    old_state: str
    new_state: str
    reason: str | None = None
    details: dict[str, str | int | float] | None = None
    event_time: datetime


class Alarm(BaseModel):
    machine_id: str
    machine_type: str
    work_center: str
    alarm_code: str
    severity: str
    message: str
    details: dict | None = None
    event_time: datetime


class CycleCompletion(BaseModel):
    machine_id: str
    machine_type: str
    work_center: str
    cycle_time_sec: float
    part_count: int
    tool_id: str | None = None
    details: dict[str, str | int | float] | None = None
    event_time: datetime


class MachineSpec(BaseModel):
    machine_id: str
    machine_type: str
    work_center: str
    model: str
    install_date: str  # ISO date


class FleetSync(BaseModel):
    machines: list[MachineSpec]
    init_state: str = "idle"
    init_time: str = "2024-01-01 00:00:00"


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok", "service": "equipment"}


@app.post("/reset")
def reset():
    """Сброс machine_status к исходному состоянию idle. Вызывается симулятором при /restart."""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE machine_status SET current_state = 'idle', "
                    "state_changed_at = '2024-01-01 00:00:00', "
                    "sensor_updated_at = '2024-01-01 00:00:00'"
                )
        logger.info("reset_ok")
        return {"reset": True}
    except Exception as exc:
        logger.error("reset_failed", extra={"details": {"error": str(exc)}})
        return {"reset": False, "error": str(exc)}


@app.post("/register-machines")
def register_machines(payload: FleetSync):
    """Синхронизация парка станков с config.MACHINES симулятора.

    В одной транзакции:
      1. Удаляются machine_status строки для машин, отсутствующих в payload (освобождение FK).
      2. Удаляются machines, отсутствующие в payload.
      3. UPSERT присланных машин (INSERT ON CONFLICT DO UPDATE).
      4. INSERT machine_status для новых машин (ON CONFLICT DO NOTHING).
      5. UPDATE machine_status: всем машинам ставится init_state и init_time.
    Полностью идемпотентно: повторный вызов с тем же payload не меняет данные.
    """
    if not payload.machines:
        return {"sync": False, "error": "empty_payload"}
    ids = [m.machine_id for m in payload.machines]
    rows = [(m.machine_id, m.machine_type, m.work_center, m.model, m.install_date)
            for m in payload.machines]
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # 1+2: убрать всё, чего нет в payload
                cur.execute("DELETE FROM machine_status WHERE machine_id != ALL(%s)", (ids,))
                deleted_status = cur.rowcount
                cur.execute("DELETE FROM machines WHERE machine_id != ALL(%s)", (ids,))
                deleted_machines = cur.rowcount
                # 3: UPSERT
                cur.executemany(
                    """
                    INSERT INTO machines (machine_id, machine_type, work_center, model, install_date)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (machine_id) DO UPDATE SET
                        machine_type = EXCLUDED.machine_type,
                        work_center  = EXCLUDED.work_center,
                        model        = EXCLUDED.model,
                        install_date = EXCLUDED.install_date
                    """,
                    rows,
                )
                # 4: добавить machine_status для машин, у которых его ещё нет
                cur.executemany(
                    """
                    INSERT INTO machine_status (machine_id, current_state, state_changed_at, sensor_updated_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (machine_id) DO NOTHING
                    """,
                    [(mid, payload.init_state, payload.init_time, payload.init_time) for mid in ids],
                )
                # 5: всем машинам — init_state и init_time
                cur.execute(
                    "UPDATE machine_status SET current_state = %s, state_changed_at = %s, sensor_updated_at = %s",
                    (payload.init_state, payload.init_time, payload.init_time),
                )
        logger.info(
            "fleet_synced",
            extra={"details": {
                "count": len(ids),
                "deleted_machines": deleted_machines,
                "deleted_status": deleted_status,
            }},
        )
        return {"sync": True, "count": len(ids),
                "deleted_machines": deleted_machines, "deleted_status": deleted_status}
    except Exception as exc:
        logger.error("fleet_sync_failed", extra={"details": {"error": str(exc)}})
        return {"sync": False, "error": str(exc)}


@app.post("/sensor-reading")
def sensor_reading(data: SensorReading):
    logger.info(
        "sensor_reading",
        extra={
            "entity_id": data.machine_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "machine_type": data.machine_type,
                "work_center": data.work_center,
                "product_code": data.product_code,
                "readings": data.readings,
            },
        },
    )
    persisted = db_update_sensor_timestamp(data.machine_id, data.event_time)
    return {"accepted": True, "machine_id": data.machine_id, "db_persisted": persisted}


@app.post("/sensor-reading/batch")
def sensor_reading_batch(batch: list[SensorReading]):
    """Принимает пачку показаний. В Loki уходит каждое отдельно (для аналитики),
    в БД — один UPDATE на машину с последним event_time."""
    latest_by_machine: dict[str, datetime] = {}
    for data in batch:
        logger.info(
            "sensor_reading",
            extra={
                "entity_id": data.machine_id,
                "event_time": data.event_time.isoformat(),
                "details": {
                    "machine_type": data.machine_type,
                    "work_center": data.work_center,
                    "product_code": data.product_code,
                    "readings": data.readings,
                },
            },
        )
        existing = latest_by_machine.get(data.machine_id)
        if existing is None or data.event_time > existing:
            latest_by_machine[data.machine_id] = data.event_time

    persisted = db_update_sensor_timestamps_batch(latest_by_machine)
    return {"accepted": True, "count": len(batch), "db_persisted": persisted}


@app.post("/state-change")
def state_change(data: StateChange):
    logger.info(
        "state_change",
        extra={
            "entity_id": data.machine_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "machine_type": data.machine_type,
                "work_center": data.work_center,
                "old_state": data.old_state,
                "new_state": data.new_state,
                "reason": data.reason,
                "extra": data.details,
            },
        },
    )
    persisted = db_update_machine_state(data.machine_id, data.new_state, data.event_time)
    return {"accepted": True, "machine_id": data.machine_id, "db_persisted": persisted}


@app.post("/alarm")
def alarm(data: Alarm):
    logger.info(
        "alarm",
        extra={
            "entity_id": data.machine_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "machine_type": data.machine_type,
                "work_center": data.work_center,
                "alarm_code": data.alarm_code,
                "severity": data.severity,
                "message": data.message,
                "extra": data.details,
            },
        },
    )
    return {"accepted": True, "machine_id": data.machine_id}


@app.post("/cycle-completion")
def cycle_completion(data: CycleCompletion):
    logger.info(
        "cycle_completion",
        extra={
            "entity_id": data.machine_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "machine_type": data.machine_type,
                "work_center": data.work_center,
                "cycle_time_sec": data.cycle_time_sec,
                "part_count": data.part_count,
                "tool_id": data.tool_id,
                "extra": data.details,
            },
        },
    )
    return {"accepted": True, "machine_id": data.machine_id}


@app.post("/cycle-completion/batch")
def cycle_completion_batch(batch: list[CycleCompletion]):
    """Принимает пачку cycle_completion. БД не трогает (cycle_completion только в логи)."""
    for data in batch:
        logger.info(
            "cycle_completion",
            extra={
                "entity_id": data.machine_id,
                "event_time": data.event_time.isoformat(),
                "details": {
                    "machine_type": data.machine_type,
                    "work_center": data.work_center,
                    "cycle_time_sec": data.cycle_time_sec,
                    "part_count": data.part_count,
                    "tool_id": data.tool_id,
                    "extra": data.details,
                },
            },
        )
    return {"accepted": True, "count": len(batch)}
