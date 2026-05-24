"""Order — заказ от клиента."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Order:
    order_id: str
    product_code: str
    total_quantity: int
    priority: str
    batch_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None