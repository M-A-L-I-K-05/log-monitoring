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


logger = setup_logging("quality")

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


app = FastAPI(title="Quality Service", lifespan=lifespan)


class Measurement(BaseModel):
    batch_id: str
    part_id: str            # человекочитаемый идентификатор детали (для логов)
    part_index: int         # числовой индекс детали в партии (для БД)
    product_code: str
    stage: str              # этап, после которого деталь меряется
    machine_id: str         # станок, на котором деталь обрабатывалась
    work_center: str        # для совместимости с прежним форматом (то же, что stage)
    parameter: str
    value: float
    nominal: float
    tolerance: float
    unit: str
    result: str             # pass | fail
    reason: str | None = None
    source_machine_id: str | None = None
    scenario_id: str | None = None
    event_time: datetime


class InspectionResult(BaseModel):
    part_id: str
    batch_id: str
    work_center: str
    decision: str
    reason: str | None = None
    inspector_id: str | None = None
    event_time: datetime


class ScenarioEvent(BaseModel):
    event: str                  # "start" | "stop" | "auto_completed"
    scenario_id: str
    machine_id: str
    scenario_type: str
    severity: str | None = None
    parts_limit: int | None = None
    details: dict | None = None
    event_time: datetime


def db_insert_measurement(data: Measurement) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO measurements (
                        batch_id, part_index, product_code, stage, machine_id,
                        parameter, value, nominal, tolerance, unit,
                        result, reason, source_machine_id, scenario_id, measured_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        data.batch_id, data.part_index, data.product_code,
                        data.stage, data.machine_id, data.parameter,
                        data.value, data.nominal, data.tolerance, data.unit,
                        data.result, data.reason, data.source_machine_id,
                        data.scenario_id, data.event_time,
                    ),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": data.part_id,
                "event_time": data.event_time.isoformat(),
                "details": {
                    "operation": "insert_measurement",
                    "error": str(exc),
                },
            },
        )
        return False


def db_insert_measurements_batch(items: list[Measurement]) -> bool:
    if not items:
        return True
    try:
        rows = [
            (m.batch_id, m.part_index, m.product_code, m.stage, m.machine_id,
             m.parameter, m.value, m.nominal, m.tolerance, m.unit,
             m.result, m.reason, m.source_machine_id, m.scenario_id, m.event_time)
            for m in items
        ]
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO measurements (
                        batch_id, part_index, product_code, stage, machine_id,
                        parameter, value, nominal, tolerance, unit,
                        result, reason, source_machine_id, scenario_id, measured_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "details": {
                    "operation": "insert_measurements_batch",
                    "count": len(items),
                    "error": str(exc),
                },
            },
        )
        return False


def _log_measurement(data: Measurement) -> None:
    logger.info(
        "measurement",
        extra={
            "entity_id": data.part_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "batch_id": data.batch_id,
                "part_index": data.part_index,
                "product_code": data.product_code,
                "stage": data.stage,
                "machine_id": data.machine_id,
                "work_center": data.work_center,
                "parameter": data.parameter,
                "value": data.value,
                "nominal": data.nominal,
                "tolerance": data.tolerance,
                "unit": data.unit,
                "result": data.result,
                "reason": data.reason,
                "source_machine_id": data.source_machine_id,
                "scenario_id": data.scenario_id,
            },
        },
    )


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok", "service": "quality"}


@app.post("/reset")
def reset():
    """Очистка таблицы measurements. Вызывается симулятором при /restart."""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE measurements RESTART IDENTITY")
        logger.info("reset_ok")
        return {"reset": True}
    except Exception as exc:
        logger.error("reset_failed", extra={"details": {"error": str(exc)}})
        return {"reset": False, "error": str(exc)}


@app.post("/measurement")
def measurement(data: Measurement):
    _log_measurement(data)
    persisted = db_insert_measurement(data)
    return {"accepted": True, "part_id": data.part_id, "db_persisted": persisted}


@app.post("/measurement/batch")
def measurement_batch(batch: list[Measurement]):
    """Принимает пачку измерений. Пишет в логи (для ML) + в БД (для Grafana)."""
    for data in batch:
        _log_measurement(data)
    persisted = db_insert_measurements_batch(batch)
    return {"accepted": True, "count": len(batch), "db_persisted": persisted}


@app.post("/scenario-event")
def scenario_event(data: ScenarioEvent):
    logger.info(
        "scenario_event",
        extra={
            "entity_id": data.scenario_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "event": data.event,
                "machine_id": data.machine_id,
                "scenario_type": data.scenario_type,
                "severity": data.severity,
                "parts_limit": data.parts_limit,
                "extra": data.details,
            },
        },
    )
    return {"accepted": True, "scenario_id": data.scenario_id}


@app.post("/inspection-result")
def inspection_result(data: InspectionResult):
    logger.info(
        "inspection_result",
        extra={
            "entity_id": data.part_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "batch_id": data.batch_id,
                "work_center": data.work_center,
                "decision": data.decision,
                "reason": data.reason,
                "inspector_id": data.inspector_id,
            },
        },
    )
    return {"accepted": True, "part_id": data.part_id}
