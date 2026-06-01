"""Configuration: все константы симулятора в одном месте."""
import os
import random
from datetime import datetime

# ─── Реальное время ────────────────────────────────────────────
TICK_REAL_SEC = 0.1          # 100 мс на тик
HTTP_TIMEOUT_SEC = 10.0

# ─── Виртуальное время ─────────────────────────────────────────
SIM_START_TIME = datetime(2026, 1, 1, 8, 0, 0)
DEFAULT_SPEED = 1.0
ALLOWED_SPEEDS = [1, 10, 100, 300, 1000]

# ─── Перемотка вперёд (кнопки +N мин) ──────────────────────────
# «Скачок» виртуального времени с генерацией всех логов: цикл прокручивает
# подсистемы шагами по FAST_FORWARD_STEP_SEC виртуальных секунд (подсистемы
# догоняют sensor_readings через свои while-циклы), реальный sleep пропускается.
# Шаг 60с безопасен: симулятор штатно работает на 1000x (шаг 100с/тик).
ADVANCE_ALLOWED_MIN = [1, 10, 30]
FAST_FORWARD_STEP_SEC = 60.0

# ─── Общие тайминги (виртуальные секунды) ──────────────────────
SETUP_TIME_SEC = 420         # 7 минут наладки (обрабатывающие станки: смена резца/фрезы)
INSPECTION_SETUP_TIME_SEC = 120  # 2 минуты для M-GMM: закрепление детали + калибровка зонда
COOLDOWN_TIME_SEC = 300      # 5 минут после партии до idle (только обрабатывающие)
SENSOR_INTERVAL_SEC = 15     # между sensor_readings одного станка

# ─── Тайминги обработки на участках (на одну деталь) ───────────
CYCLE_TIME_SEC = {
    "turning":    240,   # 4 мин
    "hobbing":    600,   # 10 мин — узкое место
    "shaving":    120,   # 2 мин
    "grinding":   180,   # 3 мин
    "inspection": 60,    # 1 мин на инспектируемую (10% выборка) — fallback
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
ORDER_INTERVAL_MIN_RANGE = (240, 480)  # 4–8 виртуальных часов
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
    # Очередь на измерение перед M-GMM (используется после каждого этапа):
    "queue_measurement",
]

# ─── Печь ──────────────────────────────────────────────────────
FURNACE_CAPACITY_PARTS = 160
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
# Этапов с измерением 5 (turning, hobbing, shaving, heat_treatment, grinding) +
# финальная инспекция. На каждом этапе проверка одной детали с фоновым
# fail-rate 0.3%. Общий фон по маршруту ≈ 1.5%.
BACKGROUND_FAIL_RATE = 0.003
SPOT_CHECK_NEIGHBORS_ON_FAIL = 2      # сколько соседних проверить если fail

# ─── Спецификации измерений по product_code ───────────────────
# Класс точности AGMA Q10–Q11 (ISO 6–7) для всех типоразмеров.
# Формат спеки — кортеж:
#   ("deviation", nominal, tolerance, unit)
#     → отклонение от номинала: pass если value in [nominal, nominal+tolerance]
#       (для геометрии: nominal=0, измеряется модуль отклонения от линии зуба).
#   ("range", min, max, unit)
#     → диапазонный параметр (твёрдость, слой): pass если value in [min, max].
#
# При генерации:
#   - deviation: значение генерируется в [nominal+0.1*tol, nominal+0.95*tol]
#   - range:     значение генерируется в [min+0.2*(max-min), max-0.2*(max-min)]
#   При fail (фон или сценарий) — за границу с заданным направлением.
MEASUREMENT_SPECS_BY_PRODUCT = {
    "SPUR-S": {
        "blank_runout":         ("deviation", 0.0, 20.0, "um"),
        "profile_deviation":    ("deviation", 0.0, 11.0, "um"),
        "lead_deviation":       ("deviation", 0.0, 12.0, "um"),
        "pitch_deviation":      ("deviation", 0.0, 10.0, "um"),
        "runout":               ("deviation", 0.0, 16.0, "um"),
        "surface_hardness_hrc": ("range", 58.0, 62.0, "HRC"),
        "core_hardness_hrc":    ("range", 30.0, 40.0, "HRC"),
        "case_depth_mm":        ("range", 0.8, 1.2, "mm"),
        "surface_roughness":    ("deviation", 0.0, 1.0,  "um"),
    },
    "SPUR-M": {  # baseline
        "blank_runout":         ("deviation", 0.0, 22.0, "um"),
        "profile_deviation":    ("deviation", 0.0, 14.0, "um"),
        "lead_deviation":       ("deviation", 0.0, 16.0, "um"),
        "pitch_deviation":      ("deviation", 0.0, 12.0, "um"),
        "runout":               ("deviation", 0.0, 22.0, "um"),
        "surface_hardness_hrc": ("range", 58.0, 62.0, "HRC"),
        "core_hardness_hrc":    ("range", 30.0, 40.0, "HRC"),
        "case_depth_mm":        ("range", 0.8, 1.2, "mm"),
        "surface_roughness":    ("deviation", 0.0, 1.2,  "um"),
    },
    "HEL-M": {
        "blank_runout":         ("deviation", 0.0, 22.0, "um"),
        "profile_deviation":    ("deviation", 0.0, 14.0, "um"),
        "lead_deviation":       ("deviation", 0.0, 14.0, "um"),
        "pitch_deviation":      ("deviation", 0.0, 12.0, "um"),
        "runout":               ("deviation", 0.0, 22.0, "um"),
        "surface_hardness_hrc": ("range", 58.0, 62.0, "HRC"),
        "core_hardness_hrc":    ("range", 30.0, 40.0, "HRC"),
        "case_depth_mm":        ("range", 0.8, 1.2, "mm"),
        "surface_roughness":    ("deviation", 0.0, 1.2,  "um"),
    },
    "HEL-L": {
        "blank_runout":         ("deviation", 0.0, 28.0, "um"),
        "profile_deviation":    ("deviation", 0.0, 18.0, "um"),
        "lead_deviation":       ("deviation", 0.0, 18.0, "um"),
        "pitch_deviation":      ("deviation", 0.0, 14.0, "um"),
        "runout":               ("deviation", 0.0, 30.0, "um"),
        "surface_hardness_hrc": ("range", 58.0, 62.0, "HRC"),
        "core_hardness_hrc":    ("range", 30.0, 40.0, "HRC"),
        "case_depth_mm":        ("range", 0.8, 1.2, "mm"),
        "surface_roughness":    ("deviation", 0.0, 1.4,  "um"),
    },
}
ALL_MEASUREMENT_PARAMS = list(MEASUREMENT_SPECS_BY_PRODUCT["SPUR-M"].keys())

# ─── Направления выхода значения за допуск ─────────────────────
# Для каждого параметра разрешённое направление при fail:
#   "up"   — только за верхнюю границу,
#   "down" — только за нижнюю,
#   "both" — в любую сторону.
# Геометрические отклонения всегда растут (износ, биение и т.п.).
# Твёрдость/слой могут уходить в обе стороны, но физически чаще ↓
# (недостаточная цементация/закалка).
PARAM_FAIL_DIRECTION = {
    "blank_runout":         "up",
    "profile_deviation":    "up",
    "pitch_deviation":      "up",
    "lead_deviation":       "up",
    "runout":               "up",
    "surface_roughness":    "up",
    "surface_hardness_hrc": "both",
    "core_hardness_hrc":    "both",
    "case_depth_mm":        "both",
}

# ─── Таблица A: какие параметры меряются после каждого этапа ──
# «Новые» = этап их создаёт. «Перемер» = этап физически влияет, проверяем повторно.
# Финальная inspection — все 8 параметров (полный контроль).
STAGE_MEASUREMENTS = {
    "turning":        ["blank_runout"],
    "hobbing":        ["profile_deviation", "pitch_deviation",
                       "lead_deviation", "runout"],
    "shaving":        ["profile_deviation", "lead_deviation"],
    "heat_treatment": ["surface_hardness_hrc", "core_hardness_hrc",
                       "case_depth_mm", "runout", "lead_deviation"],
    "grinding":       ["surface_roughness", "profile_deviation",
                       "runout", "surface_hardness_hrc"],
    "inspection":     ["blank_runout", "profile_deviation", "pitch_deviation",
                       "lead_deviation", "runout", "surface_hardness_hrc",
                       "core_hardness_hrc", "case_depth_mm", "surface_roughness"],
}

# ─── Тайминги измерения по этапам (виртуальные секунды на 1 деталь) ─
# Время-на-деталь на M-GMM (KLINGELNBERG-P26). Чем больше параметров проверяем
# и сложнее этап — тем дольше. Финальная inspection — самая длительная,
# проверяется полный набор по всем 8 параметрам.
INSPECTION_TIME_PER_PART_SEC = {
    "turning":        60,    # 1 мин — только blank_runout
    "hobbing":        240,   # 4 мин — 4 геометрических параметра
    "shaving":        120,   # 2 мин — 2 параметра
    "heat_treatment": 360,   # 6 мин — твёрдость + слой + перемер геометрии
    "grinding":       240,   # 4 мин — 4 параметра + контроль hardness
    "inspection":     480,   # 8 мин — полный контроль по 9 параметрам
}

# ─── Режимы сценариев и параметры дрейфа ────────────────────────
# gradual: медленный дрейф через границы партий (для предиктивного ML/SPC).
# step:    мгновенная полная аномалия на parts_cap деталей (для диагностики).
#
# Пороги для gradual:
#   0 → scrap_threshold: сенсоры ползут, брака нет (зона раннего предупреждения).
#   scrap_threshold → stop_threshold: пошли теги; сценарий завершается по
#   stop_threshold ИЛИ концу текущей партии.
#
# Расчёт pace = DRIFT_SCRAP_THRESHOLD / 120:
#   фаза 1 занимает ~120 деталей (2-3 партии) → длинная зона без брака.
#   фаза 2: (1.0 - 0.85) / pace ≈ 21 деталь (в пределах 10-25).
DRIFT_SCRAP_THRESHOLD = 0.85
DRIFT_STOP_THRESHOLD = 1.0
# Темп дрейфа по severity: сколько деталей нужно до scrap_threshold.
# light → медленная деградация (~180 дет), gross → быстрая (~60 дет).
DRIFT_PACE_BY_SEVERITY = {
    "light": DRIFT_SCRAP_THRESHOLD / 180,   # ≈ 0.00472
    "clear": DRIFT_SCRAP_THRESHOLD / 120,   # ≈ 0.00708
    "gross": DRIFT_SCRAP_THRESHOLD / 60,    # ≈ 0.01417
}

# Лимит деталей для step-режима: parts_cap[severity] ± STEP_PARTS_CAP_JITTER.
STEP_PARTS_CAP = {
    "light": 15,
    "clear": 10,
    "gross": 5,
}
STEP_PARTS_CAP_JITTER = 2

# ─── Печные сценарии: тайминги дрейфа и окна поимки ─────────────
# GRADUAL (under/over_carburizing): печь обрабатывает деталь ПАРТИЕЙ, поэтому
# дрейф привязан не к деталям, а ко времени фазы carburizing. Дрейф растёт
# каждые FURNACE_DRIFT_STEP_MIN минут (240/5 = 48 шагов на фазу), а не одной
# большой ступенью на этап. За одну фазу carburizing дрейф прибавляет
# FURNACE_GRADUAL_DRIFT_PER_LOAD[severity] и сохраняется между загрузками.
#
# Severity = за сколько загрузок печи дойдём до отбраковки (light медленнее,
# т.к. деградация долгая): gross — отбраковка на 2-й загрузке, clear — на 3-й,
# light — на 4-й. На загрузке отбраковки дрейф пересекает scrap (0.85) и
# доходит до stop (1.0) ВНУТРИ той же фазы carburizing: остаток 0.15
# укладывается в фазу (десятки минут < 240), печь не успевает сменить этап —
# поэтому отдельная задержка перед обслуживанием не нужна.
FURNACE_DRIFT_STEP_MIN = 5
FURNACE_GRADUAL_DRIFT_PER_LOAD = {
    "gross": 0.55,   # L1=0.55 → на L2 дрейф пересекает 0.85 и доходит до 1.0
    "clear": 0.38,   # L1=0.38, L2=0.76 → отбраковка на L3
    "light": 0.27,   # L1=0.27, L2=0.54, L3=0.81 → отбраковка на L4
}

# STEP (quench_distortion): аномалия в фазе quenching (30 мин). Две временные
# подфазы внутри quenching:
#   elapsed < CATCH_MIN  → фаза "normal": ещё можно остановить (брака нет);
#   elapsed >= CATCH_MIN → фаза "scrap": партия уходит в брак.
# Вероятности исхода НЕТ: без внешней поимки партия ГАРАНТИРОВАННО уходит в
# брак (отбраковка в [CATCH_MIN, SCRAP_MAX_MIN] мин, все детали в брак, печь →
# обслуживание; суммарно ≤ 30 мин). «Поимку» в окне [0, CATCH_MIN) можно
# инициировать только внешним вызовом FurnaceSubsystem.catch_step_scenario
# (задел под ML/диагностику через endpoint equipment/maintenance).
FURNACE_STEP_CATCH_MIN = 8
FURNACE_STEP_SCRAP_MAX_MIN = 28

# ─── Сценарии аномалий (Таблица B) ─────────────────────────────
# Каждый сценарий: имя_причины → {
#   "mode":        "gradual"|"step" — режим развития аномалии,
#   "sensors":     dict сенсор → множитель (от нормы; >1 = рост, <1 = падение),
#   "measurements": dict параметр → "up"|"down"|"both" (направление выхода
#                  за допуск; должно быть совместимо с PARAM_FAIL_DIRECTION),
#   "wo_duration_min": длительность ремонта (для maintenance WO),
# }
# Реестр организован по типу станка — UI берёт только подходящие сценарию.
SCENARIOS_BY_MACHINE_TYPE = {
    # TURNING (1)
    "turning": {
        "tool_wear": {
            "mode": "gradual",
            # Targets рассчитаны по 3σ: при drift=DRIFT_SCRAP_THRESHOLD значение
            # сенсора достигает mean ± 3σ (граница SPC). sensor_scale НЕ применяется.
            "sensors": {
                "vibration_rms_mm_s": 1.66,   # mean=0.8, std=0.15 → 3σ при drift=0.85
                "spindle_load_percent": 1.39,  # mean=45, std=5
                "spindle_bearing_temp": 1.24,  # mean=45, std=3
            },
            "measurements": {"blank_runout": "up"},
            "wo_duration_min": 25,
        },
    },
    # HOBBING (3)
    "hobbing": {
        "hob_wear": {
            "mode": "gradual",
            "sensors": {
                "vibration_rms_mm_s": 1.63,   # mean=1.4, std=0.25
                "spindle_load_percent": 1.34,  # mean=62, std=6
                "hob_bearing_temp": 1.26,      # mean=55, std=4
            },
            "measurements": {
                "profile_deviation": "up",
                "pitch_deviation": "up",
            },
            "wo_duration_min": 35,
        },
        "chatter": {
            "mode": "step",
            # Step targets гарантируют выход за 3σ даже при light severity (sensor_scale=0.6).
            "sensors": {
                "vibration_rms_mm_s": 1.90,   # было 1.80
                "spindle_load_percent": 1.49,  # было 1.25
            },
            "measurements": {"profile_deviation": "up"},
            "wo_duration_min": 30,
        },
        "workpiece_loose": {
            "mode": "step",
            "sensors": {
                "vibration_rms_mm_s": 1.90,   # было 1.50
                "work_spindle_rpm": 1.63,      # было 1.10
            },
            "measurements": {
                "runout": "up",
                "lead_deviation": "up",
            },
            "wo_duration_min": 20,
        },
    },
    # SHAVING (1)
    "shaving": {
        "shaver_wear": {
            "mode": "gradual",
            "sensors": {
                "vibration_rms_mm_s": 1.88,   # mean=0.8, std=0.20
                "spindle_load_percent": 1.47,  # mean=30, std=4
            },
            "measurements": {
                "profile_deviation": "up",
                "lead_deviation": "up",
            },
            "wo_duration_min": 25,
        },
    },
    # FURNACE (3)
    # Печные сценарии привязаны к trigger_phase: anomaly_modifier применяется
    # ТОЛЬКО во время этой фазы (см. furnace._generate_furnace_readings).
    # Gradual (under/over_carburizing): дрейф растёт по времени в carburizing
    #   (см. FURNACE_GRADUAL_DRIFT_PER_LOAD). sensor_scale НЕ применяется —
    #   severity влияет только на число загрузок до отбраковки.
    #   Targets по 3σ (как у остальных gradual): при drift=scrap_threshold
    #   значение сенсора = mean ± 3σ. target = 1 ± (3σ/mean)/0.85.
    # Step (quench_distortion): полная аномалия в quenching; sensor_scale по severity.
    "furnace": {
        "under_carburizing": {
            "mode": "gradual",
            "trigger_phase": "carburizing",
            "sensors": {
                "carbon_potential": 0.840,     # mean=1.10, std=0.05, ↓ (3σ при drift=0.85)
                "furnace_temp_zone1": 0.9848,  # mean=927, std=4, ↓
                "furnace_temp_zone2": 0.9848,
                "furnace_temp_zone3": 0.9848,
            },
            "measurements": {
                "surface_hardness_hrc": "down",
                "case_depth_mm": "down",
            },
            "wo_duration_min": 180,
        },
        "over_carburizing": {
            "mode": "gradual",
            "trigger_phase": "carburizing",
            "sensors": {
                "carbon_potential": 1.160,     # mean=1.10, std=0.05, ↑ (3σ при drift=0.85)
                "furnace_temp_zone1": 1.0152,  # mean=927, std=4, ↑
                "furnace_temp_zone2": 1.0152,
                "furnace_temp_zone3": 1.0152,
            },
            "measurements": {
                "surface_hardness_hrc": "up",
                "case_depth_mm": "up",
                "core_hardness_hrc": "up",
            },
            "wo_duration_min": 150,
        },
        "quench_distortion": {
            "mode": "step",
            "trigger_phase": "quenching",
            "sensors": {
                "quench_oil_flow": 0.40,
                "quench_oil_temp": 1.30,
            },
            "measurements": {
                "runout": "up",
                "lead_deviation": "up",
            },
            "wo_duration_min": 120,
        },
    },
    # GRINDING (3)
    "grinding": {
        "grinding_chatter": {
            "mode": "step",
            "sensors": {
                "vibration_rms_mm_s": 2.25,   # было 1.80; mean=0.6, std=0.15
                "wheel_bearing_temp": 1.32,    # было 1.15; mean=47, std=3
            },
            "measurements": {
                "surface_roughness": "up",
                "profile_deviation": "up",
            },
            "wo_duration_min": 30,
        },
        "coolant_loss": {
            "mode": "step",
            "sensors": {
                "coolant_flow": 0.30,
                "coolant_temp_c": 1.40,    # mean=21, std=1.5 → +3.4σ при light
            },
            "measurements": {
                "surface_roughness": "up",
                "runout": "up",
            },
            "wo_duration_min": 25,
        },
        "grinding_burn": {
            "mode": "step",
            "sensors": {
                "coolant_flow": 0.55,
                "spindle_power_kw": 2.00,      # было 1.80; mean=10, std=2
            },
            "measurements": {
                "surface_hardness_hrc": "down",
                "surface_roughness": "up",
            },
            "wo_duration_min": 40,
        },
    },
}

# Сценарии, связанные с износом инструмента: после их WO tool_wear сбрасывается в 0.
TOOL_SCENARIO_TYPES = {"tool_wear", "hob_wear", "shaver_wear"}

# ─── Severity: уровни тяжести сценария ─────────────────────────
# Тяжесть масштабирует:
#   1) силу искажения сенсоров вокруг 1.0 (множитель → 1 + (mult-1)*sensor_scale),
#   2) величину выхода измерения за границу допуска (фактор × ширина допуска).
# Доля брака внутри окна сценария — всегда 100% (если параметр вышел за
# допуск, деталь бракуется по определению).
SEVERITY_LEVELS = {
    "light":  {"sensor_scale": 0.6,  "measure_excess": (0.05, 0.25)},
    "clear":  {"sensor_scale": 1.0,  "measure_excess": (0.25, 0.60)},
    "gross":  {"sensor_scale": 1.5,  "measure_excess": (0.60, 1.20)},
}
DEFAULT_SEVERITY = "clear"

# Дефолтный фоновый excess: фон — это "лёгкий" выход за допуск.
BACKGROUND_FAIL_EXCESS = (0.05, 0.25)

# ─── Авто-режим сценариев (опционально) ─────────────────────────
# Когда включён: ScenariosController.tick() сам периодически запускает
# случайный сценарий на случайном подходящем станке (running/setup/cooldown
# с current_batch_id, без активного сценария).
# Самозавершение и ремонт — те же, что при ручном запуске.
#
# Обоснование интервала 360–1080 виртуальных минут (6–18 ч):
#   - средний цикл партии 80 деталей ≈ 15–20ч; интервал даёт 1–4 сценария
#     в виртуальные сутки → ~10–20% измерений с причиной + 80% baseline,
#     что хорошо ложится в задачу обучения (сенсор → дефект),
#   - между сценариями станок успевает завершить хотя бы одну партию
#     в норме — ML получает чистые сэмплы для «здорового» класса,
#   - не слишком часто → нет коллизий двух сценариев на одном станке.
AUTO_SCENARIOS_ENABLED = True
AUTO_SCENARIOS_INTERVAL_MIN_RANGE = (360, 1080)  # 6–18 виртуальных часов
AUTO_SCENARIOS_SEVERITY_WEIGHTS = [
    ("light", 0.30),
    ("clear", 0.50),
    ("gross", 0.20),
]
AUTO_SCENARIOS_PARTS_LIMIT_RANGE = (10, 30)
# Если не нашли подходящего станка — повтор через короткий backoff,
# чтобы не ждать ещё 6 часов.
AUTO_SCENARIOS_RETRY_MIN_RANGE = (15, 45)

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

# ─── Генерация сенсорного шума ─────────────────────────────────
# Усечение гауссова шума по ±SENSOR_NOISE_CLIP_SIGMA·std (truncated normal).
# Обычный random.gauss даёт ~0.3% выбросов за 3σ на каждый сенсор — на парке
# из десятков сенсоров это регулярно превышало порог ML (ANOMALY_Z=2.8) и
# давало ЛОЖНЫЕ аномалии даже в норме. Усечение ниже порога (2.5 < 2.8) →
# нормальные показания физически не дотягивают до аномалии, а сценарии
# (anomaly_modifier применяется СНАРУЖИ, после шума) по-прежнему уходят далеко.
SENSOR_NOISE_CLIP_SIGMA = float(os.environ.get("SIM_NOISE_CLIP_SIGMA", "2.5"))


def bounded_gauss(mean: float, std: float, clip_sigma: float = None) -> float:
    """Гауссов шум, усечённый по ±clip_sigma·std (rejection sampling).

    Убирает редкие выбросы-«скачки» за 3σ. Дрейф/сценарий накладывается
    отдельно (умножением на anomaly_modifier), усечение его не трогает.
    """
    if std <= 0:
        return mean
    cs = SENSOR_NOISE_CLIP_SIGMA if clip_sigma is None else clip_sigma
    while True:
        z = random.gauss(0.0, 1.0)
        if -cs <= z <= cs:
            return mean + z * std


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
