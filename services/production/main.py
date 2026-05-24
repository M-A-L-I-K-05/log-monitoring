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


logger = setup_logging("production")

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


app = FastAPI(title="Production Service", lifespan=lifespan)


def db_insert_batch(batch_id: str, order_id: str, product_code: str, priority: str,
                    work_center: str, planned_quantity: int, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO active_batches (
                        batch_id, order_id, product_code, priority, current_wc,
                        planned_quantity, actual_quantity, started_at, wc_entered_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (batch_id, order_id, product_code, priority, work_center,
                     planned_quantity, planned_quantity, event_time, event_time),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": batch_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "insert_batch",
                    "error": str(exc),
                },
            },
        )
        return False


def db_update_batch_quantity(batch_id: str, actual_quantity: int, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE active_batches SET actual_quantity = %s WHERE batch_id = %s",
                    (actual_quantity, batch_id),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": batch_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "update_batch_quantity",
                    "error": str(exc),
                },
            },
        )
        return False


def db_delete_batch(batch_id: str, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM active_batches WHERE batch_id = %s",
                    (batch_id,),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": batch_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "delete_batch",
                    "error": str(exc),
                },
            },
        )
        return False


def db_update_batch_move(batch_id: str, to_center: str, event_time: datetime) -> bool:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE active_batches SET current_wc = %s, wc_entered_at = %s WHERE batch_id = %s",
                    (to_center, event_time, batch_id),
                )
        return True
    except Exception as exc:
        logger.error(
            "db_error",
            extra={
                "entity_id": batch_id,
                "event_time": event_time.isoformat(),
                "details": {
                    "operation": "update_batch_move",
                    "error": str(exc),
                },
            },
        )
        return False


class OrderCreation(BaseModel):
    order_id: str
    product_code: str
    quantity: int
    priority: str = "normal"
    event_time: datetime


class BatchStart(BaseModel):
    batch_id: str
    order_id: str
    product_code: str
    priority: str = "normal"
    work_center: str
    planned_quantity: int
    event_time: datetime


class BatchCompletion(BaseModel):
    batch_id: str
    work_center: str
    actual_quantity: int
    defect_count: int = 0
    duration_sec: float
    event_time: datetime


class BatchMove(BaseModel):
    batch_id: str
    from_center: str
    to_center: str
    event_time: datetime


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok", "service": "production"}


@app.post("/reset")
def reset():
    """Очистка active_batches. Вызывается симулятором при /restart."""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE active_batches")
        logger.info("reset_ok")
        return {"reset": True}
    except Exception as exc:
        logger.error("reset_failed", extra={"details": {"error": str(exc)}})
        return {"reset": False, "error": str(exc)}


@app.post("/order-creation")
def order_creation(data: OrderCreation):
    logger.info(
        "order_creation",
        extra={
            "entity_id": data.order_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "product_code": data.product_code,
                "quantity": data.quantity,
                "priority": data.priority,
            },
        },
    )
    return {"accepted": True, "order_id": data.order_id}


@app.post("/batch-start")
def batch_start(data: BatchStart):
    logger.info(
        "batch_start",
        extra={
            "entity_id": data.batch_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "order_id": data.order_id,
                "product_code": data.product_code,
                "priority": data.priority,
                "work_center": data.work_center,
                "planned_quantity": data.planned_quantity,
            },
        },
    )
    persisted = db_insert_batch(
        data.batch_id,
        data.order_id,
        data.product_code,
        data.priority,
        data.work_center,
        data.planned_quantity,
        data.event_time,
    )
    return {"accepted": True, "batch_id": data.batch_id, "db_persisted": persisted}


@app.post("/batch-completion")
def batch_completion(data: BatchCompletion):
    logger.info(
        "batch_completion",
        extra={
            "entity_id": data.batch_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "work_center": data.work_center,
                "actual_quantity": data.actual_quantity,
                "defect_count": data.defect_count,
                "duration_sec": data.duration_sec,
            },
        },
    )
    persisted = db_update_batch_quantity(data.batch_id, data.actual_quantity, data.event_time)
    if data.work_center == "inspection":
        persisted = db_delete_batch(data.batch_id, data.event_time) and persisted
    return {"accepted": True, "batch_id": data.batch_id, "db_persisted": persisted}


@app.post("/batch-move")
def batch_move(data: BatchMove):
    logger.info(
        "batch_move",
        extra={
            "entity_id": data.batch_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "from_center": data.from_center,
                "to_center": data.to_center,
            },
        },
    )
    persisted = db_update_batch_move(data.batch_id, data.to_center, data.event_time)
    return {"accepted": True, "batch_id": data.batch_id, "db_persisted": persisted}