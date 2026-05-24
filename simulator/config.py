"""Configuration: все константы симулятора в одном месте."""
import os
from datetime import datetime

# ─── Реальное время ────────────────────────────────────────────
TICK_REAL_SEC = 0.1          # 100 мс на тик
HTTP_TIMEOUT_SEC = 2.0

# ─── Виртуальное время ─────────────────────────────────────────
SIM_START_TIME = datetime(2026, 1, 1, 8, 0, 0)
DEFAULT_SPEED = 1.0
ALLOWED_SPEEDS = [1, 10, 100, 1000]

# ─── Общие тайминги (виртуальные секунды) ──────────────────────
SETUP_TIME_SEC = 420         # 7 минут наладки
COOLDOWN_TIME_SEC = 300      # 5 минут после партии до idle
SENSOR_INTERVAL_SEC = 15     # между sensor_readings одного станка

# ─── Тайминги обработки на участках (на одну деталь) ───────────
CYCLE_TIME_SEC = {
    "turning":    240,   # 4 мин
    "hobbing":    600,   # 10 мин — узкое место
    "shaving":    120,   # 2 мин
    "grinding":   180,   # 3 мин
    "inspection": 60,    # 1 мин на инспектируемую (10% выборка)
}

# ─── Износ инструмента за один цикл (0.0..1.0) ─────────────────
TOOL_WEAR_PER_CYCLE = {
    "turning":  0.0040,
    "hobbing":  0.0025,
    "shaving":  0.0050,
    "grinding": 0.0033,
    "inspection": 0.0,
}
TOOL_WEAR_TRIGGER = 0.95     # триггер для maintenance work_order

# ─── Заказы ────────────────────────────────────────────────────
ORDER_INTERVAL_MIN_RANGE = (40, 80)  # 4–8 виртуальных часов
ORDER_SIZES = [
    (100, 0.70),
    (200, 0.20),
    (300,   0.10),
]
BATCH_SIZE_BY_PRIORITY = {
    "rush":   25,
    "urgent": 50,
    "normal": 80,
}
PRIORITY_WEIGHTS = [
    ("normal", 0.75),
    ("urgent", 0.20),
    ("rush",   0.05),
]
PRIORITY_ORDER = {"rush": 0, "urgent": 1, "normal": 2}
PRODUCT_WEIGHTS = [
    ("SPUR-S", 0.35),
    ("SPUR-M", 0.30),
    ("HEL-M",  0.20),
    ("HEL-L",  0.15),
]

# ─── Физические параметры типоразмеров шестерён ────────────────
# Модуль (мм), делительный диаметр (мм), угол наклона (°).
# Источник: документ дня 4 (vkr_day-04_factory-model).
PRODUCT_SPECS = {
    "SPUR-S": {"module": 1.0, "diameter": 45.0,  "helix": 0.0},
    "SPUR-M": {"module": 2.0, "diameter": 80.0,  "helix": 0.0},
    "HEL-M":  {"module": 2.0, "diameter": 80.0,  "helix": 15.0},
    "HEL-L":  {"module": 3.0, "diameter": 160.0, "helix": 20.0},
}

# ─── Модификаторы времени цикла по product_code ────────────────
# Множитель к CYCLE_TIME_SEC[machine_type]. baseline = SPUR-M.
# Hobbing: модуль 3 требует ниже скорость резания + больше объём металла
# → цикл удлиняется. Шлифование/токарка тоже зависят, но слабее.
# Shaving и inspection почти не зависят от модуля.
CYCLE_TIME_MULT_BY_PRODUCT = {
    "SPUR-S": {"turning": 0.7,  "hobbing": 0.5,  "shaving": 0.9, "grinding": 0.7,  "inspection": 0.9},
    "SPUR-M": {"turning": 1.0,  "hobbing": 1.0,  "shaving": 1.0, "grinding": 1.0,  "inspection": 1.0},
    "HEL-M":  {"turning": 1.0,  "hobbing": 1.1,  "shaving": 1.0, "grinding": 1.1,  "inspection": 1.05},
    "HEL-L":  {"turning": 1.3,  "hobbing": 1.5,  "shaving": 1.1, "grinding": 1.4,  "inspection": 1.15},
}

# ─── Модификаторы сенсорных параметров по product_code ─────────
# Применяются поверх SENSOR_PROFILES при генерации значений.
# Логика: большая шестерня → выше нагрузка/вибрация/температура.
# Косозубые при hobbing/grinding нагружают станок чуть сильнее (наклонный зуб
# даёт переменное сечение стружки → выше вибрация).
SENSOR_MODIFIERS_BY_PRODUCT = {
    "SPUR-S": {
        "spindle_load_percent": 0.70,
        "vibration_rms_mm_s":   0.80,
        "spindle_bearing_temp": 0.95,
        "hob_bearing_temp":     0.95,
        "wheel_bearing_temp":   0.95,
        "spindle_power_kw":     0.65,
    },
    "SPUR-M": {},  # baseline
    "HEL-M": {
        "spindle_load_percent": 1.08,
        "vibration_rms_mm_s":   1.10,
        "spindle_bearing_temp": 1.02,
        "hob_bearing_temp":     1.03,
        "wheel_bearing_temp":   1.03,
        "spindle_power_kw":     1.05,
    },
    "HEL-L": {
        "spindle_load_percent": 1.30,
        "vibration_rms_mm_s":   1.20,
        "spindle_bearing_temp": 1.08,
        "hob_bearing_temp":     1.10,
        "wheel_bearing_temp":   1.10,
        "spindle_power_kw":     1.35,
    },
}

# ─── Маршрут (единый для всех продуктов) ───────────────────────
ROUTE = ["turning", "hobbing", "shaving", "heat_treatment", "grinding", "inspection"]
NEXT_STAGE = {ROUTE[i]: ROUTE[i + 1] for i in range(len(ROUTE) - 1)}
NEXT_STAGE["inspection"] = "done"

# Очередь, в которую партия становится ПЕРЕД данным участком.
QUEUE_BEFORE = {
    "turning":        "pending",
    "hobbing":        "queue_hobbing",
    "shaving":        "queue_shaving",
    "heat_treatment": "waiting_furnace",
    "grinding":       "queue_grinding",
    "inspection":     "queue_inspection",
}
ALL_QUEUE_KEYS = [
    "pending", "queue_hobbing", "queue_shaving",
    "waiting_furnace", "queue_grinding", "queue_inspection",
]

# ─── Печь ──────────────────────────────────────────────────────
FURNACE_CAPACITY_PARTS = 200
FURNACE_MIN_FILL_RATIO = 0.6
FURNACE_MAX_WAIT_MIN = 30
FURNACE_PHASE_DURATIONS_SEC = {
    "loading":     5  * 60,     #   5 мин
    "heating":     120 * 60,    # 120 мин
    "carburizing": 240 * 60,    # 240 мин
    "quenching":   30 * 60,     #  30 мин
    "tempering":   90 * 60,     #  90 мин
    "unloading":   5  * 60,     #   5 мин
}
FURNACE_NEXT_PHASE = {
    "loading":     "heating",
    "heating":     "carburizing",
    "carburizing": "quenching",
    "quenching":   "tempering",
    "tempering":   "unloading",
    "unloading":   "empty",
}

# ─── Maintenance ───────────────────────────────────────────────
MAINTENANCE_CYCLES_THRESHOLD = 500
MAINTENANCE_HOURS_THRESHOLD = 72
WO_DURATION_MIN_RANGE = (30, 60)
BRIGADES = ["B-MECH-01", "B-MECH-02", "B-MECH-03"]

# ─── Quality ───────────────────────────────────────────────────
INSPECTION_SAMPLE_RATIO = 0.10        # 10% выборка на финальной инспекции
BACKGROUND_FAIL_RATE = 0.015          # 1.5% фоновый брак
MEASUREMENTS_PER_PART_RANGE = (3, 5)  # сколько параметров измеряем
# Точки промежуточного контроля (spot-check)
SPOT_CHECK_STAGES = ["hobbing", "heat_treatment"]
SPOT_CHECK_NEIGHBORS_ON_FAIL = 2      # сколько соседних проверить если fail

# ─── Спецификации измерений по product_code ───────────────────
# Класс точности AGMA Q10–Q11 (ISO 6–7) для всех типоразмеров.
# Допуски масштабируются по физике: profile ∝ модуль^0.4, runout ∝ √D,
# pitch ∝ модуль^0.3, lead ∝ ширина зуба (≈ модуль), Ra после шлифования
# почти одинакова но для крупных шестерен чуть хуже.
# Формат: (nominal, tolerance, unit). Реальное измеряемое значение —
# nominal + случайный шум в пределах tolerance.
# Для lead_deviation на косозубых — nominal не 0, а угол наклона
# (но измеряем ОТКЛОНЕНИЕ от номинала, поэтому семантика та же — допуск
# в мкм отклонения от заданной линии зуба).
MEASUREMENT_SPECS_BY_PRODUCT = {
    "SPUR-S": {
        "profile_deviation": (0.0, 11.0, "um"),
        "lead_deviation":    (0.0, 12.0, "um"),
        "pitch_deviation":   (0.0, 10.0, "um"),
        "runout":            (0.0, 16.0, "um"),
        "surface_roughness": (0.0, 1.0,  "um"),
    },
    "SPUR-M": {  # baseline (соответствует исходным TOLERANCES)
        "profile_deviation": (0.0, 14.0, "um"),
        "lead_deviation":    (0.0, 16.0, "um"),
        "pitch_deviation":   (0.0, 12.0, "um"),
        "runout":            (0.0, 22.0, "um"),
        "surface_roughness": (0.0, 1.2,  "um"),
    },
    "HEL-M": {
        # косозубая: lead жёстче (контроль угла наклона критичнее),
        # остальное близко к SPUR-M
        "profile_deviation": (0.0, 14.0, "um"),
        "lead_deviation":    (0.0, 14.0, "um"),
        "pitch_deviation":   (0.0, 12.0, "um"),
        "runout":            (0.0, 22.0, "um"),
        "surface_roughness": (0.0, 1.2,  "um"),
    },
    "HEL-L": {
        # большая косозубая: всё мягче (физически сложнее держать точность
        # на крупных шестернях при том же классе AGMA)
        "profile_deviation": (0.0, 18.0, "um"),
        "lead_deviation":    (0.0, 18.0, "um"),
        "pitch_deviation":   (0.0, 14.0, "um"),
        "runout":            (0.0, 30.0, "um"),
        "surface_roughness": (0.0, 1.4,  "um"),
    },
}
ALL_MEASUREMENT_PARAMS = list(MEASUREMENT_SPECS_BY_PRODUCT["SPUR-M"].keys())

# ─── Парк станков ─────────────────────────────────────────────
# (machine_id, machine_type, work_center, model)
MACHINES = [
    ("M-LATHE-01", "turning",    "turning",        "CKE6150"),
    ("M-LATHE-02", "turning",    "turning",        "CKE6150"),
    ("M-LATHE-03", "turning",    "turning",        "CKE6150Z"),
    ("M-HOB-01",   "hobbing",    "hobbing",        "YK3132"),
    ("M-HOB-02",   "hobbing",    "hobbing",        "YK3132"),
    ("M-HOB-03",   "hobbing",    "hobbing",        "YK3140"),
    ("M-HOB-04",   "hobbing",    "hobbing",        "YK3140"),
    ("M-SHV-01",   "shaving",    "shaving",        "YA4232CNC"),
    ("M-SHV-02",   "shaving",    "shaving",        "YA4232CNC"),
    ("M-FRN-01",   "furnace",    "heat_treatment", "IPSEN-VFC-624"),
    ("M-FRN-02",   "furnace",    "heat_treatment", "IPSEN-VFC-624"),
    ("M-GRD-01",   "grinding",   "grinding",       "REISHAUER-RZ260"),
    ("M-GRD-02",   "grinding",   "grinding",       "REISHAUER-RZ260"),
    ("M-GRD-03",   "grinding",   "grinding",       "REISHAUER-RZ260"),
    ("M-GMM-01",   "inspection", "inspection",     "KLINGELNBERG-P26"),
    ("M-GMM-02",   "inspection", "inspection",     "KLINGELNBERG-P26"),
]
INSTALL_DATE = "2024-01-01"

# ─── Сенсорные профили (mean, std, unit) ──────────────────────
# Используются и для нормальной генерации, и для определения структуры данных.
SENSOR_PROFILES = {
    "turning": {
        "spindle_load_percent": (45.0, 5.0,  "%"),
        "spindle_rpm":          (1800.0, 50.0, "rpm"),
        "feed_rate_mm_min":     (220.0, 10.0, "mm/min"),
        "coolant_temp_c":       (28.0, 1.5,  "C"),
        "vibration_rms_mm_s":   (0.8, 0.15,  "mm/s"),
        "spindle_bearing_temp": (45.0, 3.0, "C"),
    },
    "hobbing": {
        "hob_spindle_rpm":      (600.0, 30.0, "rpm"),
        "work_spindle_rpm":     (40.0, 5.0,  "rpm"),
        "axial_feed_rate":      (1.5, 0.2,   "mm/rev"),
        "spindle_load_percent": (62.0, 6.0,  "%"),
        "vibration_rms_mm_s":   (1.4, 0.25,  "mm/s"),
        "hob_bearing_temp":     (55.0, 4.0, "C"),
        "cutting_oil_temp":     (32.0, 2.0, "C"),
        "cutting_oil_flow":     (100.0, 5.0, "L/min"),
    },
    "shaving": {
        "spindle_rpm":          (350.0, 20.0, "rpm"),
        "spindle_load_percent": (30.0, 4.0,  "%"),
        "vibration_rms_mm_s":   (0.8, 0.2, "mm/s"),
        "spindle_bearing_temp": (45.0, 3.0, "C"),
    },
    "grinding": {
        "wheel_spindle_rpm":    (4500.0, 100.0, "rpm"),
        "work_spindle_rpm":     (120.0, 15.0, "rpm"),
        "spindle_power_kw":     (10.0, 2.0, "kW"),
        "vibration_rms_mm_s":   (0.6, 0.15, "mm/s"),
        "wheel_bearing_temp":   (47.0, 3.0, "C"),
        "coolant_flow":         (100.0, 5.0, "L/min"),
        "coolant_temp_c":       (21.0, 1.5, "C"),
    },
    "inspection": {
        "ambient_temp":     (20.0, 0.3, "C"),
        "ambient_humidity": (50.0, 2.0, "%"),
        "air_pressure":     (6.0, 0.3, "bar"),
    },
}

# ─── Профили сенсоров печи (по фазам) ─────────────────────────
FURNACE_SENSOR_PROFILES = {
    "loading": {
        "furnace_temp_zone1":      (30.0, 5.0, "C"),
        "furnace_temp_zone2":      (30.0, 5.0, "C"),
        "furnace_temp_zone3":      (30.0, 5.0, "C"),
        "atmosphere_pressure":     (1.00, 0.01, "atm"),
    },
    "heating": {
        "furnace_temp_zone1":      (760.0, 15.0, "C"),
        "furnace_temp_zone2":      (920.0, 12.0, "C"),
        "furnace_temp_zone3":      (840.0, 12.0, "C"),
        "atmosphere_pressure":     (1.01, 0.005, "atm"),
        "carbon_potential":        (0.40, 0.05, "%C"),
    },
    "carburizing": {
        "furnace_temp_zone1":      (927.0, 4.0, "C"),
        "furnace_temp_zone2":      (927.0, 4.0, "C"),
        "furnace_temp_zone3":      (927.0, 4.0, "C"),
        "atmosphere_pressure":     (1.015, 0.003, "atm"),
        "carbon_potential":        (1.10, 0.05, "%C"),
    },
    "quenching": {
        "furnace_temp_zone1":      (843.0, 5.0, "C"),
        "furnace_temp_zone2":      (843.0, 5.0, "C"),
        "furnace_temp_zone3":      (843.0, 5.0, "C"),
        "quench_oil_temp":         (65.0, 3.0, "C"),
        "quench_oil_flow":         (300.0, 10.0, "L/min"),
    },
    "tempering": {
        "furnace_temp_zone1":      (177.0, 3.0, "C"),
        "furnace_temp_zone2":      (177.0, 3.0, "C"),
        "furnace_temp_zone3":      (177.0, 3.0, "C"),
    },
    "unloading": {
        "furnace_temp_zone1":      (177.0, 3.0, "C"),
        "furnace_temp_zone2":      (177.0, 3.0, "C"),
        "furnace_temp_zone3":      (177.0, 3.0, "C"),
    },
}

# ─── URL сервисов (из ENV) ────────────────────────────────────
SERVICE_URLS = {
    "equipment":   os.environ.get("EQUIPMENT_URL",   "http://equipment:8001"),
    "production":  os.environ.get("PRODUCTION_URL",  "http://production:8002"),
    "quality":     os.environ.get("QUALITY_URL",     "http://quality:8003"),
    "maintenance": os.environ.get("MAINTENANCE_URL", "http://maintenance:8004"),
}