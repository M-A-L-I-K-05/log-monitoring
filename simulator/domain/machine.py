"""Machine — состояние одного станка в памяти симулятора."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Machine:
    machine_id: str
    machine_type: str       # turning|hobbing|shaving|furnace|grinding|inspection
    work_center: str
    state: str = "idle"     # idle|setup|running|cooldown|maintenance|fault

    # Текущая партия
    current_batch_id: str | None = None

    # Виртуальное время моментов
    state_changed_at: datetime | None = None       # когда зашёл в текущее состояние
    last_sensor_sent_at: datetime | None = None    # последний sensor_reading

    # Износ инструмента / счётчики
    tool_wear: float = 0.0                         # 0.0..1.0
    tool_wear_alarm_sent: bool = False             # чтобы не дублировать alarm
    cycles_since_maintenance: int = 0
    last_maintenance_at: datetime | None = None

    # Счётчики по текущей партии
    parts_done_in_batch: int = 0

    # ── Хук для сценариев аномалий ──
    # Каждый ключ — имя сенсора, значение — мультипликативный модификатор.
    # Например: {"vibration_rms_mm_s": 1.5} → значение vibration умножается на 1.5
    anomaly_modifier: dict[str, float] = field(default_factory=dict)

    # ── Связь с активным сценарием (если есть) ──
    # id сценария из ScenariosController.active. Используется equipment для
    # пометки обрабатываемых деталей и quality для согласованной деформации.
    active_scenario_id: str | None = None

    # ── Случай B: сценарий захватил всю партию → следующая партия меряется
    # с 10% выборкой (вместо «1 случайной»), даже без активного сценария.
    # Сбрасывается в False после первой такой партии.
    verify_next_batch_with_sample: bool = False

    # ── Для M-GMM (inspection): какую стадию мы сейчас «обмериваем» ──
    # Когда инспекционный станок берёт партию на измерение, тут хранится тот
    # этап, после которого её отправили. Используется quality, чтобы понять,
    # промежуточный это перемер или финальный.
    measuring_after_stage: str | None = None
    # ── Инкрементальное измерение (деталь за деталью) ──
    # measurement_plan — очередь деталей к измерению: список словарей
    #   {idx, mode, scenario_id, source, force_pass}. План строится при
    #   переходе setup→running и может РАСТИ в спот-режиме: если контрольная
    #   деталь забракована, в план добавляются 2 соседние (доизмерение).
    measurement_plan: list = field(default_factory=list)
    measurement_done: int = 0       # сколько деталей уже измерено
    measurement_total: int = 0      # текущий размер плана (для прогресса)
