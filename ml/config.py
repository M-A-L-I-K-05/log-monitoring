"""Конфигурация ML-сервиса: все настройки в одном месте (ENV переопределяет)."""
import os


def _f(name, default):
    return float(os.environ.get(name, default))


def _i(name, default):
    return int(os.environ.get(name, default))


# ─── Внешние сервисы ───────────────────────────────────────────
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://admin:secret@postgres:5432/factory")

# ─── Извлечение логов из Loki ──────────────────────────────────
# ВАЖНО: Loki индексирует логи по РЕАЛЬНОМУ времени приёма (wall clock), а не
# по виртуальному времени завода. Симулятор гоняет время ускоренно, поэтому
# запрашиваем НЕДАВНЕЕ реальное окно, а ось времени ряда строим по полю
# event_time из JSON (виртуальное время). См. loki_client / features.
REAL_LOOKBACK_MIN = _f("ML_REAL_LOOKBACK_MIN", "30")   # сколько реальных минут тянуть
MAX_LOG_LINES = _i("ML_MAX_LOG_LINES", "300000")        # потолок строк за запрос
LOKI_PAGE_LIMIT = _i("ML_LOKI_PAGE_LIMIT", "5000")      # max entries на страницу Loki
LOKI_TIMEOUT_SEC = _f("ML_LOKI_TIMEOUT_SEC", "30")

# Метки потоков (см. promtail-config.yaml: relabel → service_name, labels → event/level).
SENSOR_SERVICE = "equipment"
SENSOR_EVENT = "sensor_reading"
SCENARIO_SERVICE = "quality"          # scenario_event POST'ится в quality-сервис
SCENARIO_EVENT = "scenario_event"     # разметка для /evaluate
ALARM_EVENT = "alarm"

# ─── Подготовка фичей ──────────────────────────────────────────
RESAMPLE_RULE = os.environ.get("ML_RESAMPLE", "1min")   # шаг ресемпла виртуального ряда
MIN_TRAIN_POINTS = _i("ML_MIN_TRAIN_POINTS", "30")      # минимум точек для fit

# Главные сенсоры для Prophet-прогноза по типу станка (ключевые для сценариев).
# Detection (ECOD/IForest) использует ВСЕ сенсоры станка; forecast — только эти.
MAIN_SENSORS = {
    "turning":  ["vibration_rms_mm_s", "spindle_load_percent", "spindle_bearing_temp"],
    "hobbing":  ["vibration_rms_mm_s", "spindle_load_percent", "hob_bearing_temp"],
    "shaving":  ["vibration_rms_mm_s", "spindle_load_percent"],
    "grinding": ["vibration_rms_mm_s", "wheel_bearing_temp", "coolant_flow", "spindle_power_kw"],
    "furnace":  ["carbon_potential", "furnace_temp_zone1"],
}
# Типы станков, у которых нет «аномальных» сенсоров (инспекция) — пропускаем.
SKIP_MACHINE_TYPES = {"inspection"}

# ─── Детекторы аномалий (PyOD) ─────────────────────────────────
CONTAMINATION = _f("ML_CONTAMINATION", "0.02")          # ожидаемая доля аномалий
IFOREST_RANDOM_STATE = _i("ML_IFOREST_SEED", "42")
# Как комбинировать ECOD и IForest: "any" (хотя бы один) | "both" (оба согласны).
ANOMALY_COMBINE = os.environ.get("ML_COMBINE", "any")

# ─── Forecasting (Prophet) ─────────────────────────────────────
PROPHET_INTERVAL_WIDTH = _f("ML_PROPHET_INTERVAL", "0.99")
FORECAST_HORIZON_MIN = _i("ML_FORECAST_HORIZON_MIN", "30")
# Прогноз тяжелее детекции (Prophet fit на ряд) — в фоне делаем реже.
FORECAST_EVERY_RUNS = _i("ML_FORECAST_EVERY_RUNS", "5")

# ─── Хранение весов на диске (volume) ──────────────────────────
# Веса детекторов сохраняются как версии в MODELS_DIR (монтируется как
# named volume → переживают пересборку/перезапуск контейнера). Обучение —
# явное (через UI/endpoint); скоринг идёт по загруженной активной версии.
MODELS_DIR = os.environ.get("ML_MODELS_DIR", "/app/models")

# ─── Фоновый поток ─────────────────────────────────────────────
BACKGROUND_ENABLED = os.environ.get("ML_BACKGROUND_ENABLED", "true").lower() == "true"
BACKGROUND_INTERVAL_SEC = _f("ML_BACKGROUND_INTERVAL_SEC", "120")
# По умолчанию фон ТОЛЬКО скорит по сохранённым весам и НЕ переобучает —
# веса замораживаются обучением на чистом baseline. Авто-переобучение можно
# включить тумблером (на случай дрейфа парка): retrain каждые N прогонов.
BACKGROUND_RETRAIN_ENABLED = os.environ.get(
    "ML_BACKGROUND_RETRAIN_ENABLED", "false").lower() == "true"
RETRAIN_EVERY_RUNS = _i("ML_RETRAIN_EVERY_RUNS", "10")

# ─── Оценка качества (/evaluate) ───────────────────────────────
# Допуск по времени при сопоставлении аномалии с окном сценария (виртуальные минуты).
EVAL_TOLERANCE_MIN = _f("ML_EVAL_TOLERANCE_MIN", "5")
