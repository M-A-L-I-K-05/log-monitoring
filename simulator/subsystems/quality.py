"""QualitySubsystem: контроль качества.

Три точки контроля:
1. Spot-check после hobbing — 1 случайная деталь + 2 соседних если fail
2. Spot-check после heat_treatment — то же самое
3. Финальная inspection — 10% выборка с полным набором измерений

Equipment-подсистема регистрирует задачи:
- state.pending_spot_checks — (batch_id, stage) для spot-check
- state.pending_inspection_measurements — (batch_id, part_idx, machine_id)
"""
import logging
import random
from datetime import datetime

import config

logger = logging.getLogger("simulator.quality")


class QualitySubsystem:
    name = "quality"

    INSPECTOR_ID = "M-GMM"  # для spot-check виртуальный инспектор

    def __init__(self, state, client):
        self.state = state
        self.client = client

    def tick(self, now: datetime) -> None:
        # 1. Обработка spot-check заявок
        with self.state.lock:
            while self.state.pending_spot_checks:
                batch_id, stage = self.state.pending_spot_checks.popleft()
                batch = self.state.batches.get(batch_id)
                if batch is None:
                    continue
                self._do_spot_check(batch, stage, now)

            # 2. Обработка измерений для финальной инспекции
            while self.state.pending_inspection_measurements:
                batch_id, part_idx, machine_id, event_time = self.state.pending_inspection_measurements.popleft()
                batch = self.state.batches.get(batch_id)
                if batch is None:
                    continue
                self._do_inspection(batch, part_idx, machine_id, event_time)

    # ─── spot-check после промежуточной стадии ────────────────
    def _do_spot_check(self, batch, stage: str, now: datetime) -> None:
        """Берём случайную деталь, делаем 2-3 измерения. Если fail — ещё 2 соседних."""
        first_idx = random.randint(1, batch.quantity)
        decision = self._measure_part(batch, first_idx, stage, now)
        if decision == "fail":
            # проверяем 2 соседних (всегда pass в нормальной работе)
            for neigh_offset in range(1, config.SPOT_CHECK_NEIGHBORS_ON_FAIL + 1):
                neigh_idx = first_idx + neigh_offset
                if neigh_idx > batch.quantity:
                    neigh_idx = first_idx - neigh_offset
                if 1 <= neigh_idx <= batch.quantity:
                    self._measure_part(batch, neigh_idx, stage, now, force_pass=True)
        batch.spot_checked_at.add(stage)

    # ─── финальная инспекция одной детали ─────────────────────
    def _do_inspection(self, batch, part_idx: int, machine_id: str,
                       now: datetime) -> None:
        self._measure_part(batch, part_idx, "inspection", now,
                           inspector_id=machine_id)

    # ─── общий метод измерения одной детали ───────────────────
    def _measure_part(self, batch, part_idx: int, work_center: str,
                      now: datetime, force_pass: bool = False,
                      inspector_id: str | None = None) -> str:
        part_id = f"P-{batch.batch_id}-{part_idx:04d}"
        n_meas = random.randint(*config.MEASUREMENTS_PER_PART_RANGE)
        params = random.sample(config.ALL_MEASUREMENT_PARAMS,
                               k=min(n_meas, len(config.ALL_MEASUREMENT_PARAMS)))

        # Спеки зависят от типоразмера шестерни (разные модули/диаметры дают разные
        # абсолютные допуски при одном классе точности AGMA Q10–Q11).
        specs = config.MEASUREMENT_SPECS_BY_PRODUCT.get(
            batch.product_code, config.MEASUREMENT_SPECS_BY_PRODUCT["SPUR-M"]
        )

        any_out_of_tolerance = False
        for param in params:
            lo, hi, unit = specs[param]
            # значение в норме (60–95% от верхней границы для реалистичности)
            value = random.uniform(lo + 0.1 * (hi - lo), hi * 0.95)
            nominal = (lo + hi) / 2.0
            tolerance = (hi - lo) / 2.0
            self.client.measurement(
                batch_id=batch.batch_id,
                part_id=part_id,
                work_center=work_center,
                parameter=param,
                value=round(value, 4),
                nominal=round(nominal, 4),
                tolerance=round(tolerance, 4),
                unit=unit,
                event_time=now,
            )
            # выход за допуск — в нормальном режиме почти не бывает
            if value < lo or value > hi:
                any_out_of_tolerance = True

        # решение pass/fail
        if force_pass:
            decision = "pass"
        elif any_out_of_tolerance:
            decision = "fail"
        elif random.random() < config.BACKGROUND_FAIL_RATE:
            decision = "fail"
        else:
            decision = "pass"

        if decision == "fail":
            batch.fails_count += 1
            self.state.counters["inspections_fail"] += 1
        else:
            self.state.counters["inspections_pass"] += 1

        self.client.inspection_result(
            part_id=part_id,
            batch_id=batch.batch_id,
            work_center=work_center,
            decision=decision,
            event_time=now,
            inspector_id=inspector_id or self.INSPECTOR_ID,
            reason=("background_random" if decision == "fail" else None),
        )
        return decision