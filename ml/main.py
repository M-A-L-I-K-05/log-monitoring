"""ML-сервис (FastAPI, :8006) — детекция аномалий + прогноз трендов по логам.

Pipeline: Loki (structured JSON) → pandas → PyOD (ECOD + IsolationForest) +
Prophet → PostgreSQL (ml_anomalies / ml_forecasts / ml_runs) → Grafana.

Веса детекторов замораживаются обучением и хранятся на диске (volume) как
версии — см. model_store. Интерактивное управление (обучение, переключение
версий, режимы) — через WebUI на :8006 и одноимённые endpoint'ы.
"""
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pythonjsonlogger import jsonlogger

import config
import loki_client
import store
from pipeline import Pipeline


def setup_logging(service_name: str) -> logging.Logger:
    formatter = jsonlogger.JsonFormatter(
        "%(levelname)s %(name)s %(message)s",
        rename_fields={"levelname": "level", "name": "service", "message": "event"},
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = False
        uv.disabled = True
    return logging.getLogger(service_name)


logger = setup_logging("ml")
pipeline = Pipeline()


class _Background:
    """Фоновый поток. По умолчанию ТОЛЬКО скорит по загруженным весам.

    Переобучение в фоне выключено (веса заморожены явным обучением). Его можно
    включить тумблером retrain_enabled — тогда раз в RETRAIN_EVERY_RUNS прогонов
    детекторы переобучаются на свежем окне и сохраняются новой версией.
    """

    def __init__(self):
        self.enabled = config.BACKGROUND_ENABLED
        self.interval = config.BACKGROUND_INTERVAL_SEC
        self.retrain_enabled = config.BACKGROUND_RETRAIN_ENABLED
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._runs_since_train = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ml-bg")
        self._thread.start()

    def _loop(self):
        # дать стеку прогреться (Loki/симулятор могут ещё подниматься)
        self._stop.wait(10)
        while not self._stop.is_set():
            if self.enabled:
                try:
                    if self.retrain_enabled and (
                            not pipeline.detectors or
                            self._runs_since_train >= config.RETRAIN_EVERY_RUNS):
                        pipeline.train(tag="auto-retrain")
                        self._runs_since_train = 0
                    if pipeline.detectors:
                        pipeline.run_once()
                        self._runs_since_train += 1
                    else:
                        logger.info("background_idle", extra={"details": {
                            "reason": "нет активной обученной модели"}})
                except Exception as exc:
                    logger.error("background_run_failed",
                                 extra={"details": {"error": str(exc)}})
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def state(self) -> dict:
        return {"enabled": self.enabled, "interval_sec": self.interval,
                "retrain_enabled": self.retrain_enabled}


bg = _Background()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init()
    bg.start()
    logger.info("ml_service_started", extra={"details": {
        "loki": config.LOKI_URL,
        "active_version": pipeline.active_version,
        "background_enabled": bg.enabled,
        "interval_sec": bg.interval}})
    try:
        yield
    finally:
        bg.stop()
        store.close()


app = FastAPI(title="ML Service", lifespan=lifespan)

WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")
if os.path.isdir(WEBUI_DIR):
    app.mount("/static", StaticFiles(directory=WEBUI_DIR), name="static")


# ─── модели запросов ─────────────────────────────────────────
class RunRequest(BaseModel):
    real_lookback_min: float | None = None
    forecast: bool | None = None


class TrainRequest(BaseModel):
    real_lookback_min: float | None = None
    contamination: float | None = None
    tag: str | None = None
    machine_ids: list[str] | None = None


class LoopRequest(BaseModel):
    enabled: bool | None = None
    interval_sec: float | None = None
    retrain_enabled: bool | None = None


class VersionRequest(BaseModel):
    version: str


# ─── endpoints: статус ───────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "ml", "loki_ready": loki_client.ping()}


@app.get("/status")
def status():
    st = pipeline.status()
    st["background"] = bg.state()
    return st


# ─── endpoints: обучение и версии весов ──────────────────────
@app.post("/train")
def train(req: TrainRequest):
    return pipeline.train(req.real_lookback_min, contamination=req.contamination,
                          tag=req.tag, machine_ids=req.machine_ids)


@app.get("/models")
def models():
    return pipeline.list_models()


@app.post("/models/activate")
def activate(req: VersionRequest):
    try:
        return pipeline.activate_version(req.version)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/models/{version}")
def delete_model(version: str):
    return pipeline.delete_version(version)


# ─── endpoints: скоринг ──────────────────────────────────────
@app.post("/run-once")
def run_once(req: RunRequest):
    return pipeline.run_once(req.real_lookback_min, req.forecast)


@app.post("/detect")
def detect(req: RunRequest):
    return pipeline.run_once(req.real_lookback_min, do_forecast=False)


@app.post("/forecast")
def forecast(req: RunRequest):
    return pipeline.run_once(req.real_lookback_min, do_forecast=True)


@app.post("/evaluate")
def evaluate(req: RunRequest):
    return pipeline.evaluate(req.real_lookback_min)


# ─── endpoints: режим и сброс ────────────────────────────────
@app.post("/loop")
def loop(req: LoopRequest):
    if req.enabled is not None:
        bg.enabled = req.enabled
    if req.interval_sec:
        bg.interval = req.interval_sec
    if req.retrain_enabled is not None:
        bg.retrain_enabled = req.retrain_enabled
    logger.info("loop_toggled", extra={"details": bg.state()})
    return bg.state()


@app.post("/reset")
def reset():
    """Чистит результаты (ml_anomalies/forecasts/runs) и счётчик. Веса на диске
    НЕ удаляются — для удаления модели используйте DELETE /models/{version}."""
    return pipeline.reset_results()


# ─── WebUI ───────────────────────────────────────────────────
@app.get("/")
def index():
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "WebUI not found", "service": "ml"}
