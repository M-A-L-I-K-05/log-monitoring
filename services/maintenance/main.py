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


logger = setup_logging("maintenance")

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


app = FastAPI(title="Maintenance Service", lifespan=lifespan)


def db_insert_work_order(wo_id: str, machine_id: str, wo_type: str, priority: str,
                         reason: str | None, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO open_work_orders (
                        wo_id, machine_id, type, priority, status, reason, created_at
                    ) VALUES (%s, %s, %s, %s, 'created', %s, %s)
                    """,
                    (wo_id, machine_id, wo_type, priority, reason, event_time),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": wo_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "insert_work_order",
                    "error": str(exc),
                },
            },
        )
        return False


def db_assign_work_order(wo_id: str, brigade_id: str, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE open_work_orders
                    SET status = 'assigned', assigned_brigade = %s, assigned_at = %s
                    WHERE wo_id = %s
                    """,
                    (brigade_id, event_time, wo_id),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": wo_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "assign_work_order",
                    "error": str(exc),
                },
            },
        )
        return False


def db_delete_work_order(wo_id: str, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM open_work_orders WHERE wo_id = %s",
                    (wo_id,),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": wo_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "delete_work_order",
                    "error": str(exc),
                },
            },
        )
        return False


class WorkOrderCreation(BaseModel):
    wo_id: str
    machine_id: str
    type: str
    priority: str
    reason: str | None = None
    event_time: datetime


class WorkOrderAssignment(BaseModel):
    wo_id: str
    brigade_id: str
    event_time: datetime


class WorkOrderCompletion(BaseModel):
    wo_id: str
    duration_min: float
    parts_replaced: list[str] = []
    event_time: datetime


class ScheduledMaintenance(BaseModel):
    machine_id: str
    type: str
    event_time: datetime


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok", "service": "maintenance"}


@app.post("/reset")
def reset():
    """Очистка open_work_orders. Вызывается симулятором при /restart."""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE open_work_orders")
        logger.info("reset_ok")
        return {"reset": True}
    except Exception as exc:
        logger.error("reset_failed", extra={"details": {"error": str(exc)}})
        return {"reset": False, "error": str(exc)}


@app.post("/work-order-creation")
def work_order_creation(data: WorkOrderCreation):
    logger.info(
        "work_order_creation",
        extra={
            "entity_id": data.wo_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "machine_id": data.machine_id,
                "type": data.type,
                "priority": data.priority,
                "reason": data.reason,
            },
        },
    )
    persisted = db_insert_work_order(data.wo_id, data.machine_id, data.type,
                                     data.priority, data.reason, data.event_time)
    return {"accepted": True, "wo_id": data.wo_id, "db_persisted": persisted}


@app.post("/work-order-assignment")
def work_order_assignment(data: WorkOrderAssignment):
    logger.info(
        "work_order_assignment",
        extra={
            "entity_id": data.wo_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "brigade_id": data.brigade_id,
            },
        },
    )
    persisted = db_assign_work_order(data.wo_id, data.brigade_id, data.event_time)
    return {"accepted": True, "wo_id": data.wo_id, "db_persisted": persisted}


@app.post("/work-order-completion")
def work_order_completion(data: WorkOrderCompletion):
    logger.info(
        "work_order_completion",
        extra={
            "entity_id": data.wo_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "duration_min": data.duration_min,
                "parts_replaced": data.parts_replaced,
            },
        },
    )
    persisted = db_delete_work_order(data.wo_id, data.event_time)
    return {"accepted": True, "wo_id": data.wo_id, "db_persisted": persisted}


@app.post("/scheduled-maintenance")
def scheduled_maintenance(data: ScheduledMaintenance):
    logger.info(
        "scheduled_maintenance",
        extra={
            "entity_id": data.machine_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "type": data.type,
            },
        },
    )
    return {"accepted": True, "machine_id": data.machine_id}