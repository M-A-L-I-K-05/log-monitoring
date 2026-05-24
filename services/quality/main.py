import logging
from datetime import datetime

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
app = FastAPI(title="Quality Service")


class Measurement(BaseModel):
    batch_id: str
    part_id: str
    work_center: str
    parameter: str
    value: float
    nominal: float
    tolerance: float
    unit: str
    event_time: datetime


class InspectionResult(BaseModel):
    part_id: str
    batch_id: str
    work_center: str
    decision: str
    reason: str | None = None
    inspector_id: str | None = None
    event_time: datetime


@app.get("/health")
def health():
    logger.info("health_check")
    return {"status": "ok", "service": "quality"}


@app.post("/measurement")
def measurement(data: Measurement):
    logger.info(
        "measurement",
        extra={
            "entity_id": data.part_id,
            "event_time": data.event_time.isoformat(),
            "details": {
                "batch_id": data.batch_id,
                "work_center": data.work_center,
                "parameter": data.parameter,
                "value": data.value,
                "nominal": data.nominal,
                "tolerance": data.tolerance,
                "unit": data.unit,
            },
        },
    )
    return {"accepted": True, "part_id": data.part_id}


@app.post("/measurement/batch")
def measurement_batch(batch: list[Measurement]):
    """Принимает пачку измерений. БД не трогает (measurement только в логи)."""
    for data in batch:
        logger.info(
            "measurement",
            extra={
                "entity_id": data.part_id,
                "event_time": data.event_time.isoformat(),
                "details": {
                    "batch_id": data.batch_id,
                    "work_center": data.work_center,
                    "parameter": data.parameter,
                    "value": data.value,
                    "nominal": data.nominal,
                    "tolerance": data.tolerance,
                    "unit": data.unit,
                },
            },
        )
    return {"accepted": True, "count": len(batch)}


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