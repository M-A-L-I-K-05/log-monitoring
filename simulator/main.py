"""FastAPI control API симулятора."""
import logging
import os
import threading

import docker as docker_sdk
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
scenarios = ScenariosController(state, client=client)
orders_sub = OrdersSubsystem(state, client)
quality_sub = QualitySubsystem(state, client)

subsystems = [
    orders_sub,
    ProductionDispatcher(state, client),
    EquipmentSubsystem(state, client, quality_sub),
    FurnaceSubsystem(state, client),
    quality_sub,
    MaintenanceSubsystem(state, client),
    scenarios,
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


class StartScenarioRequest(BaseModel):
    machine_id: str
    scenario_type: str
    severity: str = config.DEFAULT_SEVERITY
    parts_limit: int = 30


class StopScenarioRequest(BaseModel):
    scenario_id: str


class AutoScenariosRequest(BaseModel):
    enabled: bool


class CreateOrderRequest(BaseModel):
    product_code: str
    priority: str = "normal"
    total_quantity: int = 100


class AutoOrdersRequest(BaseModel):
    enabled: bool


# ─── endpoints ───────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "simulator", "alive": loop.is_alive()}


@app.get("/status")
def get_status():
    snap = state.snapshot()
    snap["active_scenarios"] = scenarios.list_active()
    snap["auto_orders"] = orders_sub.get_auto_status()
    return snap


@app.post("/start")
def start():
    loop.start()
    return {"ok": True, "running": state.running}


@app.post("/stop")
def stop():
    loop.stop()
    return {"ok": True, "running": state.running}


def _clear_loki_async() -> None:
    """Удаляет все данные Loki и перезапускает контейнер (в фоне)."""
    try:
        dc = docker_sdk.from_env()
        loki = dc.containers.get("loki")
        loki.exec_run("sh -c 'rm -rf /loki/chunks /loki/wal /loki/tsdb-shipper-active /loki/tsdb-shipper-cache /loki/compactor'")
        loki.restart(timeout=10)
    except Exception as exc:
        logging.getLogger("simulator.restart").warning("Loki cleanup failed: %s", exc)


@app.post("/restart")
def restart():
    loop.restart()
    threading.Thread(target=_clear_loki_async, daemon=True).start()
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


# ─── scenarios endpoints ─────────────────────────────────────
@app.get("/scenarios")
def list_scenarios():
    return {
        "active": scenarios.list_active(),
        "catalog": scenarios.catalog(),
        "severity_levels": list(config.SEVERITY_LEVELS.keys()),
        "auto": scenarios.get_auto_status(),
    }


@app.post("/scenarios/auto")
def set_auto_scenarios(req: AutoScenariosRequest):
    scenarios.set_auto_enabled(req.enabled)
    return {"ok": True, "auto": scenarios.get_auto_status()}


@app.post("/scenarios/start")
def start_scenario(req: StartScenarioRequest):
    try:
        sid = scenarios.start_scenario(
            machine_id=req.machine_id,
            scenario_type=req.scenario_type,
            severity=req.severity,
            parts_limit=req.parts_limit,
        )
        return {"ok": True, "scenario_id": sid}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/scenarios/stop")
def stop_scenario(req: StopScenarioRequest):
    ok = scenarios.stop_scenario(req.scenario_id)
    if not ok:
        raise HTTPException(status_code=404, detail="scenario not found")
    return {"ok": True}


@app.post("/scenarios/stop-all")
def scn_stop_all():
    n = scenarios.stop_all()
    return {"stopped": n}


# ─── orders endpoints ────────────────────────────────────────
@app.get("/orders/auto")
def get_auto_orders():
    return orders_sub.get_auto_status()


@app.post("/orders/auto")
def set_auto_orders(req: AutoOrdersRequest):
    orders_sub.set_auto_enabled(req.enabled)
    return {"ok": True, "auto": orders_sub.get_auto_status()}


@app.post("/orders/create")
def create_order(req: CreateOrderRequest):
    try:
        result = orders_sub.create_order(
            product_code=req.product_code,
            priority=req.priority,
            total_quantity=req.total_quantity,
            now=state.virtual_time,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─── WebUI ────────────────────────────────────────────────────
@app.get("/")
def index():
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "WebUI not found", "service": "simulator"}
