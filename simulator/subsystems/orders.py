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

    def reset(self) -> None:
        """Сброс при /restart симулятора."""
        self._next_order_at = None

    def tick(self, now: datetime) -> None:
        if self._next_order_at is None:
            self._next_order_at = now + timedelta(seconds=config.SENSOR_INTERVAL_SEC)
            return

        if now < self._next_order_at:
            return

        # пора создавать новый ордер
        order = self._generate_order(now)
        batches = self._split_into_batches(order, now)
        order.batch_ids = [b.batch_id for b in batches]

        # регистрируем в state
        with self.state.lock:
            self.state.orders[order.order_id] = order
            for b in batches:
                self.state.batches[b.batch_id] = b
                self.state.queues["pending"].append(b)
            self.state.counters["orders_total"] += 1
            self.state.counters["batches_total"] += len(batches)

        # шлём события
        self.client.order_creation(order, event_time=now)
        for b in batches:
            self.client.batch_start(b, work_center="pending", event_time=now)

        self._next_order_at = now + self._random_interval()

    # ─── helpers ──────────────────────────────────────────────
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