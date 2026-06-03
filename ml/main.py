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
        self.prophet_enabled = config.PROPHET_BACKGROUND_ENABLED
        self._tick = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ml-bg")
        self._thread.start()

    @property
    def lookback_min(self) -> float:
        """Окно Loki для скоринга = интервал фона + запас, в реальных минутах.
        Следует за текущим интервалом, поэтому смена интервала меняет и окно."""
        return (self.interval + config.SCORING_MARGIN_SEC) / 60.0

    @property
    def prophet_every(self) -> int:
        """Сколько тиков фона приходится на один прогнозный цикл Prophet.
        Цель — раз в PROPHET_CYCLE_SEC секунд: при интервале 5с и цели 30с → 6."""
        return max(1, round(config.PROPHET_CYCLE_SEC / self.interval))

    def _loop(self):
        self._stop.wait(10)
        while not self._stop.is_set():
            if self.enabled:
                self._tick += 1
                try:
                    if pipeline.detectors:
                        # детекция — каждый тик; авто-forecast здесь выключен,
                        # прогноз ведёт отдельный prophet-контур ниже.
                        pipeline.run_once(do_forecast=False,
                                          lookback_min=self.lookback_min)
                        # прогнозный цикл — реже, по счётчику
                        if self.prophet_enabled and self._tick % self.prophet_every == 0:
                            pipeline.run_prophet_cycle(lookback_min=self.lookback_min)
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
                "lookback_sec": round(self.interval + config.SCORING_MARGIN_SEC, 2),
                "prophet_enabled": self.prophet_enabled,
                "prophet_every": self.prophet_every,
                "prophet_cycle_sec": round(self.prophet_every * self.interval, 1)}


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
    forecast: bool | None = None


class TrainRequest(BaseModel):
    contamination: float | None = None
    tag: str | None = None


class LoopRequest(BaseModel):
    enabled: bool | None = None
    interval_sec: float | None = None


class ProphetLoopRequest(BaseModel):
    enabled: bool | None = None


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
    return pipeline.train(contamination=req.contamination, tag=req.tag)


@app.get("/models")
def models():
    return pipeline.list_models()


@app.post("/models/activate")
def activate(req: VersionRequest):
    try:
        return pipeline.activate_version(req.version)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/models")
def delete_all_models():
    """Удаляет ВСЕ сохранённые версии весов и снимает активную."""
    return pipeline.delete_all_versions()


@app.delete("/models/{version}")
def delete_model(version: str):
    return pipeline.delete_version(version)


# ─── endpoints: скоринг ──────────────────────────────────────
@app.post("/run-once")
def run_once(req: RunRequest):
    return pipeline.run_once(req.forecast)


@app.post("/detect")
def detect(req: RunRequest):
    return pipeline.run_once(do_forecast=False)


@app.post("/forecast")
def forecast(req: RunRequest):
    return pipeline.run_once(do_forecast=True)


@app.post("/evaluate")
def evaluate():
    return pipeline.evaluate()


# ─── endpoints: режим и сброс ────────────────────────────────
@app.post("/loop")
def loop(req: LoopRequest):
    if req.enabled is not None:
        bg.enabled = req.enabled
    if req.interval_sec is not None:
        if req.interval_sec < 1:
            raise HTTPException(status_code=400,
                                detail="интервал должен быть ≥ 1 секунды")
        bg.interval = float(req.interval_sec)
    logger.info("loop_toggled", extra={"details": bg.state()})
    return bg.state()


@app.post("/prophet_loop")
def prophet_loop(req: ProphetLoopRequest):
    """Включает/выключает фоновый прогнозный контур Prophet (карточки Grafana)."""
    if req.enabled is not None:
        bg.prophet_enabled = req.enabled
    logger.info("prophet_loop_toggled", extra={"details": bg.state()})
    return bg.state()


@app.post("/prophet_cycle")
def prophet_cycle():
    """Прогнать прогнозный цикл один раз вручную (для проверки/первого заполнения)."""
    return pipeline.run_prophet_cycle(lookback_min=bg.lookback_min)


@app.post("/reset")
def reset():
    """Чистит ВСЕ результаты ML: ml_runs, ml_anomalies, ml_forecasts,
    ml_prophet_status, ml_prophet_events, ml_scenarios — и память пайплайна
    (дедуп, фронт Prophet). Веса на диске НЕ удаляются — для удаления модели
    используйте DELETE /models/{version}."""
    return pipeline.reset_results()


# ─── WebUI ───────────────────────────────────────────────────
@app.get("/")
def index():
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "WebUI not found", "service": "ml"}
