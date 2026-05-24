"""Batch — состояние одной партии в памяти симулятора."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Batch:
    batch_id: str
    order_id: str
    product_code: str
    priority: str
    quantity: int

    # Текущая стадия
    stage: str = "pending"  # pending|turning|hobbing|shaving|waiting_furnace|heat_treatment|grinding|inspection|done
    current_machine_id: str | None = None

    created_at: datetime | None = None
    stage_started_at: datetime | None = None
    parts_done_in_stage: int = 0

    # На финальной инспекции — индексы деталей, попавших в 10% выборку
    inspection_sample_indices: set[int] = field(default_factory=set)
    inspection_sampled_done: set[int] = field(default_factory=set)

    # Какие стадии уже прошли spot-check (после hobbing, после heat_treatment)
    spot_checked_at: set[str] = field(default_factory=set)

    # Накопленный брак
    fails_count: int = 0