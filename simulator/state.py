"""SimulationState — единое состояние симулятора + виртуальные часы."""
import threading
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta

import config
from domain.machine import Machine
from domain.batch import Batch
from domain.order import Order
from domain.work_order import WorkOrder, Brigade
from domain.furnace_load import FurnaceLoad


class SimulationState:
    """Единственное место, где живёт всё состояние симулятора."""

    def __init__(self):
        # ─── Виртуальные часы (3 поля вместо VirtualClock) ──
        self.virtual_time: datetime = config.SIM_START_TIME
        self.speed: float = config.DEFAULT_SPEED
        self.running: bool = False

        # ─── Lock на всё состояние ──
        self._lock = threading.RLock()

        # ─── Парк станков ──
        self.machines: dict[str, Machine] = {}
        for mid, mtype, wc, _model in config.MACHINES:
            self.machines[mid] = Machine(
                machine_id=mid,
                machine_type=mtype,
                work_center=wc,
                state_changed_at=self.virtual_time,
                last_maintenance_at=self.virtual_time,
            )

        # ─── Бригады ──
        self.brigades: dict[str, Brigade] = {
            bid: Brigade(brigade_id=bid) for bid in config.BRIGADES
        }

        # ─── Очереди партий между участками ──
        self.queues: dict[str, deque[Batch]] = {
            key: deque() for key in config.ALL_QUEUE_KEYS
        }

        # ─── Активные сущности ──
        self.orders: dict[str, Order] = {}
        self.batches: dict[str, Batch] = {}        # все активные партии
        self.work_orders: dict[str, WorkOrder] = {}
        self.furnace_loads: dict[str, FurnaceLoad] = {}  # ключ = machine_id

        # ─── Очереди задач для подсистем ──
        # spot-check ожидает обработки Quality: [(batch_id, stage_name), ...]
        self.pending_spot_checks: deque[tuple[str, str]] = deque()
        # измерения на финальной инспекции: [(batch_id, part_idx, machine_id, event_time), ...]
        self.pending_inspection_measurements: deque[tuple[str, int, str, datetime]] = deque()
        # запросы на ТО по износу инструмента: [machine_id, ...]
        self.pending_tool_change_requests: deque[str] = deque()

        # ─── Счётчики для дашборда ──
        self.counters: dict[str, int] = {
            "orders_total": 0,
            "batches_total": 0,
            "batches_done": 0,
            "inspections_pass": 0,
            "inspections_fail": 0,
        }

        # ─── ID-генераторы ──
        self._order_seq = 0
        self._batch_seq = 0
        self._wo_seq = 0
        self._load_seq = 0

    # ─── Lock context ──────────────────────────────────────────
    @property
    def lock(self):
        return self._lock

    # ─── Виртуальное время ────────────────────────────────────
    def advance_time(self, real_seconds: float) -> None:
        """Продвинуть виртуальное время. Вызывается главным циклом каждый тик."""
        with self._lock:
            if self.running:
                self.virtual_time += timedelta(seconds=real_seconds * self.speed)

    def pause(self) -> None:
        with self._lock:
            self.running = False

    def resume(self) -> None:
        with self._lock:
            self.running = True

    def set_speed(self, speed: float) -> None:
        with self._lock:
            self.speed = speed

    # ─── Сброс к исходному состоянию ──────────────────────────
    def reset(self) -> None:
        """Привести все данные к начальному состоянию (как после __init__)."""
        with self._lock:
            self.virtual_time = config.SIM_START_TIME
            self.speed = config.DEFAULT_SPEED

            # пересоздаём станки
            self.machines.clear()
            for mid, mtype, wc, _model in config.MACHINES:
                self.machines[mid] = Machine(
                    machine_id=mid,
                    machine_type=mtype,
                    work_center=wc,
                    state_changed_at=self.virtual_time,
                    last_maintenance_at=self.virtual_time,
                )

            # пересоздаём бригады
            self.brigades.clear()
            for bid in config.BRIGADES:
                self.brigades[bid] = Brigade(brigade_id=bid)

            # чистим очереди
            for q in self.queues.values():
                q.clear()

            # чистим активные сущности
            self.orders.clear()
            self.batches.clear()
            self.work_orders.clear()
            self.furnace_loads.clear()

            # чистим очереди задач для подсистем
            self.pending_spot_checks.clear()
            self.pending_inspection_measurements.clear()
            self.pending_tool_change_requests.clear()

            # сбрасываем счётчики
            for key in self.counters:
                self.counters[key] = 0

            # сбрасываем ID-генераторы
            self._order_seq = 0
            self._batch_seq = 0
            self._wo_seq = 0
            self._load_seq = 0

    # ─── ID-генераторы ────────────────────────────────────────
    def next_order_id(self) -> str:
        with self._lock:
            self._order_seq += 1
            return f"ORD-{self.virtual_time.year}-{self._order_seq:04d}"

    def next_batch_id(self) -> str:
        with self._lock:
            self._batch_seq += 1
            return f"B-{self._batch_seq:05d}"

    def next_wo_id(self) -> str:
        with self._lock:
            self._wo_seq += 1
            return f"WO-{self._wo_seq:04d}"

    def next_load_id(self) -> str:
        with self._lock:
            self._load_seq += 1
            return f"FL-{self._load_seq:04d}"

    # ─── Снимок для /status ────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "virtual_time": self.virtual_time.isoformat(),
                "speed": self.speed,
                "running": self.running,
                "machines": [asdict(m) for m in self.machines.values()],
                "active_batches": [asdict(b) for b in self.batches.values()],
                "queues": {
                    key: [asdict(b) for b in q]
                    for key, q in self.queues.items()
                },
                "furnace_loads": [asdict(fl) for fl in self.furnace_loads.values()],
                "open_work_orders": [asdict(wo) for wo in self.work_orders.values()],
                "brigades": [asdict(b) for b in self.brigades.values()],
                "counters": dict(self.counters),
            }