"""OrdersSubsystem: генерация заказов раз в 4–8 виртуальных часов."""
import random
from datetime import datetime, timedelta

import config
from domain.order import Order
from domain.batch import Batch


def weighted_choice(items: list[tuple]) -> str:
    """[(key, weight), ...] -> key"""
    keys = [k for k, _ in items]
    weights = [w for _, w in items]
    return random.choices(keys, weights=weights, k=1)[0]


class OrdersSubsystem:
    name = "orders"

    def __init__(self, state, client):
        self.state = state
        self.client = client
        self._next_order_at: datetime | None = None
        self._auto_enabled: bool = True

    def reset(self) -> None:
        self._next_order_at = None

    def tick(self, now: datetime) -> None:
        if not self._auto_enabled:
            return

        if self._next_order_at is None:
            self._next_order_at = now + timedelta(seconds=config.SENSOR_INTERVAL_SEC)
            return

        if now < self._next_order_at:
            return

        self._place_order(now)
        self._next_order_at = now + self._random_interval()

    def set_auto_enabled(self, enabled: bool) -> None:
        self._auto_enabled = bool(enabled)
        if self._auto_enabled and self._next_order_at is None:
            self._next_order_at = self.state.virtual_time + timedelta(seconds=config.SENSOR_INTERVAL_SEC)

    def get_auto_status(self) -> dict:
        return {
            "enabled": self._auto_enabled,
            "next_at": self._next_order_at.isoformat() if self._next_order_at else None,
            "interval_min_range": list(config.ORDER_INTERVAL_MIN_RANGE),
        }

    def create_order(self, product_code: str, priority: str, total_quantity: int,
                     now: datetime) -> dict:
        """Ручное создание заказа."""
        if product_code not in dict(config.PRODUCT_WEIGHTS):
            raise ValueError(f"unknown product_code: {product_code}")
        if priority not in config.PRIORITY_ORDER:
            raise ValueError(f"unknown priority: {priority}")
        if total_quantity < 1:
            raise ValueError("total_quantity must be >= 1")

        order = Order(
            order_id=self.state.next_order_id(),
            product_code=product_code,
            total_quantity=total_quantity,
            priority=priority,
            created_at=now,
        )
        batches = self._split_into_batches(order, now)
        order.batch_ids = [b.batch_id for b in batches]

        with self.state.lock:
            self.state.orders[order.order_id] = order
            for b in batches:
                self.state.batches[b.batch_id] = b
                self.state.queues["pending"].append(b)
            self.state.counters["orders_total"] += 1
            self.state.counters["batches_total"] += len(batches)

        self.client.order_creation(order, event_time=now)
        for b in batches:
            self.client.batch_start(b, work_center="pending", event_time=now)

        return {"order_id": order.order_id, "batches": len(batches), "total_quantity": total_quantity}

    # ─── helpers ──────────────────────────────────────────────
    def _place_order(self, now: datetime) -> None:
        order = self._generate_order(now)
        batches = self._split_into_batches(order, now)
        order.batch_ids = [b.batch_id for b in batches]

        with self.state.lock:
            self.state.orders[order.order_id] = order
            for b in batches:
                self.state.batches[b.batch_id] = b
                self.state.queues["pending"].append(b)
            self.state.counters["orders_total"] += 1
            self.state.counters["batches_total"] += len(batches)

        self.client.order_creation(order, event_time=now)
        for b in batches:
            self.client.batch_start(b, work_center="pending", event_time=now)

    def _random_interval(self) -> timedelta:
        lo, hi = config.ORDER_INTERVAL_MIN_RANGE
        return timedelta(minutes=random.uniform(lo, hi))

    def _generate_order(self, now: datetime) -> Order:
        return Order(
            order_id=self.state.next_order_id(),
            product_code=weighted_choice(config.PRODUCT_WEIGHTS),
            total_quantity=weighted_choice(config.ORDER_SIZES),
            priority=weighted_choice(config.PRIORITY_WEIGHTS),
            created_at=now,
        )

    def _split_into_batches(self, order: Order, now: datetime) -> list[Batch]:
        size = config.BATCH_SIZE_BY_PRIORITY[order.priority]
        remaining = order.total_quantity
        batches: list[Batch] = []
        while remaining > 0:
            qty = min(size, remaining)
            batches.append(Batch(
                batch_id=self.state.next_batch_id(),
                order_id=order.order_id,
                product_code=order.product_code,
                priority=order.priority,
                quantity=qty,
                created_at=now,
            ))
            remaining -= qty
        return batches
