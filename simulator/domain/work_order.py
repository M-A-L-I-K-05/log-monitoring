"""WorkOrder и Brigade — для ТО."""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class WorkOrder:
    wo_id: str
    machine_id: str
    type: str                # "preventive" | "tool_wear"
    priority: str            # "normal" | "high"
    status: str = "created"  # created|assigned|completed
    reason: str | None = None
    assigned_brigade_id: str | None = None
    created_at: datetime | None = None
    assigned_at: datetime | None = None
    expected_duration_sec: float = 45 * 60  # 45 минут по умолчанию


@dataclass
class Brigade:
    brigade_id: str
    is_busy: bool = False
    current_wo_id: str | None = None