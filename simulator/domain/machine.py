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