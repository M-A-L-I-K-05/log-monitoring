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
        # Партии печи, замороженные после сценарного выброса и ожидающие
        # завершения ремонта печи (после WO → queue_measurement).
        # ключ = machine_id, значение = [batch_id, ...]
        self.frozen_furnace_batches: dict[str, list[str]] = {}

        # ─── Очереди задач для подсистем ──
        # Quality: партии, ожидающие обработки после M-GMM-измерения.
        # Equipment кладёт сюда, когда M-GMM закончил физическую обработку:
        # (batch_id, stage_after, gmm_id, event_time)
        # stage_after — этап, после которого партия пришла на измерение.
        self.pending_measurements: deque[tuple[str, str, str, datetime]] = deque()

        # Maintenance: запросы на ТО по износу инструмента: [machine_id, ...]
        self.pending_tool_change_requests: deque[str] = deque()

        # Maintenance: запросы на ремонт от завершившихся сценариев.
        # [(machine_id, scenario_type, duration_min), ...]
        self.pending_scenario_wos: deque[tuple[str, str, float]] = deque()

        # Реестр активных сценариев. ScenariosController перезаписывает его на
        # каждой регистрации/удалении сценария. Quality читает (через
        # state.scenarios_registry[scenario_id]) для согласованной деформации.
        self.scenarios_registry: dict[str, dict] = {}

        # ─── Счётчики для дашборда ──
        self.counters: dict[str, int] = {
            "orders_total": 0,
            "batches_total": 0,
            "batches_done": 0,
            "parts_pass": 0,
            "parts_fail": 0,
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
            self.frozen_furnace_batches.clear()

            # чистим очереди задач для подсистем
            self.pending_measurements.clear()
            self.pending_tool_change_requests.clear()
            self.pending_scenario_wos.clear()
            self.scenarios_registry.clear()

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
    @staticmethod
    def _batch_snapshot(b) -> dict:
        """asdict партии + вычисляемое годное количество для UI/прогресса."""
        d = asdict(b)
        d["good_quantity"] = b.effective_quantity
        return d

    def _inspection_station(self, m) -> dict:
        """Карточка измерительного станка (M-GMM) для отдельной панели UI."""
        b = self.batches.get(m.current_batch_id) if m.current_batch_id else None
        plan = m.measurement_plan or []
        if m.state == "idle":
            mode = "idle"
        elif m.state == "setup":
            mode = "setup"
        elif any(it.get("mode") == "scenario" for it in plan):
            mode = "scenario"
        elif m.measuring_after_stage == "inspection":
            mode = "final"
        elif m.measurement_total > 1:
            mode = "sample"
        elif m.measurement_total == 1:
            mode = "spot"
        else:
            mode = "—"
        return {
            "machine_id": m.machine_id,
            "state": m.state,
            "batch_id": m.current_batch_id,
            "product_code": b.product_code if b else None,
            "stage_after": m.measuring_after_stage,
            "parts_total": m.measurement_total,
            "parts_done": m.measurement_done,
            "mode": mode,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "virtual_time": self.virtual_time.isoformat(),
                "speed": self.speed,
                "running": self.running,
                "machines": [asdict(m) for m in self.machines.values()],
                "active_batches": [self._batch_snapshot(b) for b in self.batches.values()],
                "queues": {
                    key: [asdict(b) for b in q]
                    for key, q in self.queues.items()
                },
                "furnace_loads": [asdict(fl) for fl in self.furnace_loads.values()],
                "inspection_stations": [
                    self._inspection_station(m) for m in self.machines.values()
                    if m.machine_type == "inspection"
                ],
                "open_work_orders": [asdict(wo) for wo in self.work_orders.values()],
                "brigades": [asdict(b) for b in self.brigades.values()],
                "counters": dict(self.counters),
            }