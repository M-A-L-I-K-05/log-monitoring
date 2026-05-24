"""FastAPI control API симулятора."""
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from client import FactoryClient
from loop import SimulationLoop
from state import SimulationState
from subsystems.equipment import EquipmentSubsystem
from subsystems.furnace import FurnaceSubsystem
from subsystems.maintenance import MaintenanceSubsystem
from subsystems.orders import OrdersSubsystem
from subsystems.production import ProductionDispatcher
from subsystems.quality import QualitySubsystem
from subsystems.scenarios import ScenariosController


# ─── logging disable ──────────────────────────────────────────────────
def disable_logging() -> None:
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = False
        uv.disabled = True


disable_logging()

# ─── глобальное состояние ────────────────────────────────────
state = SimulationState()
client = FactoryClient()
scenarios = ScenariosController(state)

subsystems = [
    OrdersSubsystem(state, client),
    ProductionDispatcher(state, client),
    EquipmentSubsystem(state, client),
    FurnaceSubsystem(state, client),
    QualitySubsystem(state, client),
    MaintenanceSubsystem(state, client),
    scenarios,  # scenarios.tick() обрабатывает завершение по таймеру
]

loop = SimulationLoop(state, subsystems)                                                 

# ─── FastAPI ──────────────────────────────────────────────────
app = FastAPI(title="Factory Simulator")

WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")
if os.path.isdir(WEBUI_DIR):
    app.mount("/static", StaticFiles(directory=WEBUI_DIR), name="static")


# ─── Pydantic-модели запросов ────────────────────────────────
class SpeedRequest(BaseModel):
    multiplier: float


class ToolWearRequest(BaseModel):
    machine_id: str
    duration_min: float = 60.0
    intensity: float = 1.5


class BearingOverheatRequest(BaseModel):
    machine_id: str
    duration_min: float = 30.0
    intensity: float = 1.4


class FurnaceDriftRequest(BaseModel):
    machine_id: str
    zone: int = 2
    duration_min: float = 60.0
    drift_pct: float = 0.05


class CoolantFailureRequest(BaseModel):
    machine_id: str
    duration_min: float = 20.0


class QualityBurstRequest(BaseModel):
    work_center: str
    duration_min: float = 60.0
    fail_rate: float = 0.15


# ─── endpoints ───────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "simulator", "alive": loop.is_alive()}


@app.get("/status")
def get_status():
    return state.snapshot()


@app.post("/start")
def start():
    loop.start()
    return {"ok": True, "running": state.running}


@app.post("/stop")
def stop():
    loop.stop()
    return {"ok": True, "running": state.running}


@app.post("/restart")
def restart():
    loop.restart()
    return {"ok": True, "running": state.running}


@app.post("/sync-fleet")
def sync_fleet():
    """Синхронизация парка станков с equipment-сервисом.

    Берёт текущий config.MACHINES, шлёт на equipment /register-machines.
    Используется при изменении config (добавил/убрал/переименовал станок).
    Не runtime-операция — вызывается вручную через кнопку.
    """
    result = client.sync_fleet()
    return {"ok": True, "result": result}


@app.post("/speed")
def set_speed(req: SpeedRequest):
    if req.multiplier not in config.ALLOWED_SPEEDS:
        raise HTTPException(
            status_code=400,
            detail=f"speed must be one of {config.ALLOWED_SPEEDS}")
    state.set_speed(req.multiplier)
    return {"ok": True, "speed": state.speed}


# ─── scenarios endpoints (заготовки) ─────────────────────────
@app.get("/scenarios")
def list_scenarios():
    return {"active": scenarios.list_active()}


@app.post("/scenarios/tool-wear")
def scn_tool_wear(req: ToolWearRequest):
    sid = scenarios.start_tool_wear_acceleration(
        req.machine_id, req.duration_min, req.intensity)
    return {"scenario_id": sid}


@app.post("/scenarios/bearing-overheat")
def scn_bearing(req: BearingOverheatRequest):
    sid = scenarios.start_bearing_overheat(
        req.machine_id, req.duration_min, req.intensity)
    return {"scenario_id": sid}


@app.post("/scenarios/furnace-drift")
def scn_furnace(req: FurnaceDriftRequest):
    sid = scenarios.start_furnace_drift(
        req.machine_id, req.zone, req.duration_min, req.drift_pct)
    return {"scenario_id": sid}


@app.post("/scenarios/coolant-failure")
def scn_coolant(req: CoolantFailureRequest):
    sid = scenarios.start_coolant_failure(req.machine_id, req.duration_min)
    return {"scenario_id": sid}


@app.post("/scenarios/quality-burst")
def scn_quality(req: QualityBurstRequest):
    sid = scenarios.start_quality_burst(
        req.work_center, req.duration_min, req.fail_rate)
    return {"scenario_id": sid}


@app.post("/scenarios/stop-all")
def scn_stop_all():
    n = scenarios.stop_all()
    return {"stopped": n}


# ─── WebUI ────────────────────────────────────────────────────
@app.get("/")
def index():
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "WebUI not found", "service": "simulator"}