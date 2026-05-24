"""FurnaceLoad — одна загрузка печи."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FurnaceLoad:
    load_id: str
    machine_id: str
    product_codes: list[str]     # уникальные product_code в загрузке (смешивание разрешено)
    batch_ids: list[str]
    total_parts: int             # ≤ FURNACE_CAPACITY_PARTS
    phase: str = "loading"       # loading|heating|carburizing|quenching|tempering|unloading
    phase_started_at: datetime | None = None
    last_sensor_sent_at: datetime | None = None