"""ScenariosController: управление сценариями аномалий по реестру config.

Логика:
- При старте сценария: проверяем тип станка → ищем сценарий в реестре
  config.SCENARIOS_BY_MACHINE_TYPE[machine_type][scenario_type].
- Применяем sensor-модификаторы (масштабируем по severity вокруг 1.0).
- Сохраняем мета (включая measurements/severity/parts_limit) в state.scenarios_registry
  (читается equipment и quality).
- Лимит в деталях. Если parts_limit > остатка деталей в текущей партии —
  обрезается до остатка (сценарий не «доживает» до следующей партии).
- Самозавершение — equipment._auto_complete_scenario, который создаёт
  pending_scenario_wo. ScenariosController.tick() удаляет неактуальные
  сценарии (status=auto_completed) и снимает модификаторы.
"""
import logging
import random
from datetime import datetime, timedelta

import config

logger = logging.getLogger("simulator.scenarios")


class ScenariosController:
    name = "scenarios"

    def __init__(self, state, client=None):
        self.state = state
        self.client = client
        # active: scenario_id → meta. Дублируется в state.scenarios_registry
        # для удобного доступа из quality/equipment.
        self.active: dict[str, dict] = {}
        self._seq = 0
        # Авто-режим
        self._auto_enabled: bool = config.AUTO_SCENARIOS_ENABLED
        self._auto_next_at: datetime | None = None

    def reset(self) -> None:
        """Сброс при /restart симулятора."""
        # снять все модификаторы со станков
        for sid, meta in list(self.active.items()):
            mid = meta.get("machine_id")
            if mid:
                machine = self.state.machines.get(mid)
                if machine:
                    for key in meta.get("sensors", {}):
                        machine.anomaly_modifier.pop(key, None)
                    machine.active_scenario_id = None
        self.active.clear()
        self.state.scenarios_registry.clear()
        self._seq = 0
        self._auto_next_at = None
        # _auto_enabled НЕ сбрасываем: если пользователь включил его через UI,
        # restart симулятора не должен это отменять.

    def tick(self, now: datetime) -> None:
        """Уборка сценариев со статусом auto_completed/stopped + авто-генератор."""
        finished = [sid for sid, meta in self.active.items()
                    if meta.get("status") in ("auto_completed", "stopped")]
        for sid in finished:
            self._cleanup_scenario(sid, now)

        if self._auto_enabled:
            self._maybe_auto_start(now)

    # ─── Авто-режим ──────────────────────────────────────────
    def _maybe_auto_start(self, now: datetime) -> None:
        """Если пришло время — запустить случайный сценарий."""
        if self._auto_next_at is None:
            self._schedule_next_auto(now)
            return
        if now < self._auto_next_at:
            return

        # Подходящий станок: типа, для которого есть сценарии, в активном
        # состоянии, без уже активного сценария.
        candidates = [
            m for m in self.state.machines.values()
            if m.machine_type in config.SCENARIOS_BY_MACHINE_TYPE
            and m.state in ("setup", "running", "cooldown")
            and (m.current_batch_id is not None
                 or (m.machine_type == "furnace"
                     and self.state.furnace_loads.get(m.machine_id) is not None))
            and m.active_scenario_id is None
        ]
        if not candidates:
            # Подождём чуть-чуть и попробуем снова (короткий backoff).
            lo, hi = config.AUTO_SCENARIOS_RETRY_MIN_RANGE
            self._auto_next_at = now + timedelta(minutes=random.uniform(lo, hi))
            return

        machine = random.choice(candidates)
        catalog = config.SCENARIOS_BY_MACHINE_TYPE.get(machine.machine_type, {})
        if not catalog:
            self._schedule_next_auto(now)
            return

        scenario_type = random.choice(list(catalog.keys()))
        severity = self._weighted_severity()
        parts_limit = random.randint(*config.AUTO_SCENARIOS_PARTS_LIMIT_RANGE)
        try:
            sid = self.start_scenario(
                machine_id=machine.machine_id,
                scenario_type=scenario_type,
                severity=severity,
                parts_limit=parts_limit,
            )
            logger.info("[AUTO] started %s: %s on %s (sev=%s, limit=%d)",
                        sid, scenario_type, machine.machine_id, severity, parts_limit)
        except ValueError as exc:
            logger.warning("[AUTO] cannot start: %s", exc)
        finally:
            self._schedule_next_auto(now)

    def _schedule_next_auto(self, now: datetime) -> None:
        lo, hi = config.AUTO_SCENARIOS_INTERVAL_MIN_RANGE
        self._auto_next_at = now + timedelta(minutes=random.uniform(lo, hi))

    def _weighted_severity(self) -> str:
        keys = [k for k, _ in config.AUTO_SCENARIOS_SEVERITY_WEIGHTS]
        weights = [w for _, w in config.AUTO_SCENARIOS_SEVERITY_WEIGHTS]
        return random.choices(keys, weights=weights, k=1)[0]

    def set_auto_enabled(self, enabled: bool) -> None:
        """Включить/выключить авто-режим (для API + UI)."""
        self._auto_enabled = bool(enabled)
        if self._auto_enabled and self._auto_next_at is None:
            self._schedule_next_auto(self.state.virtual_time)
        elif not self._auto_enabled:
            self._auto_next_at = None
        logger.info("auto_scenarios %s", "ENABLED" if self._auto_enabled else "DISABLED")

    def get_auto_status(self) -> dict:
        return {
            "enabled": self._auto_enabled,
            "next_at": self._auto_next_at.isoformat() if self._auto_next_at else None,
            "interval_min_range": list(config.AUTO_SCENARIOS_INTERVAL_MIN_RANGE),
            "severity_weights": config.AUTO_SCENARIOS_SEVERITY_WEIGHTS,
            "parts_limit_range": list(config.AUTO_SCENARIOS_PARTS_LIMIT_RANGE),
        }

    # ─── публичный API: старт ────────────────────────────────
    def start_scenario(self, machine_id: str, scenario_type: str,
                       severity: str = config.DEFAULT_SEVERITY,
                       parts_limit: int = 30) -> str:
        machine = self.state.machines.get(machine_id)
        if machine is None:
            raise ValueError(f"machine {machine_id} not found")
        catalog = config.SCENARIOS_BY_MACHINE_TYPE.get(machine.machine_type, {})
        spec = catalog.get(scenario_type)
        if spec is None:
            raise ValueError(
                f"scenario '{scenario_type}' not available for machine_type '{machine.machine_type}'"
            )
        if severity not in config.SEVERITY_LEVELS:
            severity = config.DEFAULT_SEVERITY

        # Если на станке уже есть активный сценарий — не запускаем второй.
        if machine.active_scenario_id and machine.active_scenario_id in self.active:
            raise ValueError(
                f"machine {machine_id} already has active scenario {machine.active_scenario_id}"
            )

        # Лимит — обрезка по остатку, если возможно вычислить.
        parts_limit_effective = self._effective_limit(machine, parts_limit)

        # sensor-множители: вокруг 1.0, масштабируем по severity.
        sev = config.SEVERITY_LEVELS[severity]
        sensor_scale = sev["sensor_scale"]
        sensors_final: dict[str, float] = {}
        for k, mult in spec["sensors"].items():
            scaled = 1.0 + (mult - 1.0) * sensor_scale
            sensors_final[k] = scaled
            machine.anomaly_modifier[k] = scaled

        sid = self._next_id()
        meta = {
            "id": sid,
            "machine_id": machine_id,
            "machine_type": machine.machine_type,
            "scenario_type": scenario_type,
            "severity": severity,
            "parts_limit": parts_limit,
            "parts_limit_effective": parts_limit_effective,
            "sensors": sensors_final,
            "measurements": dict(spec.get("measurements", {})),
            "wo_duration_min": spec.get("wo_duration_min", 30.0),
            "trigger_phase": spec.get("trigger_phase"),  # для печных сценариев
            "started_at": self.state.virtual_time,
            "status": "active",
        }
        self.active[sid] = meta
        self.state.scenarios_registry[sid] = meta
        machine.active_scenario_id = sid

        logger.info("started scenario %s on %s (%s, severity=%s, limit=%d→%d)",
                    sid, machine_id, scenario_type, severity, parts_limit,
                    parts_limit_effective)
        if self.client is not None:
            self.client.scenario_event(
                event="start",
                scenario_id=sid,
                machine_id=machine_id,
                scenario_type=scenario_type,
                severity=severity,
                parts_limit=parts_limit_effective,
                event_time=self.state.virtual_time,
                details={"sensors": sensors_final,
                         "measurements": meta["measurements"],
                         "wo_duration_min": meta["wo_duration_min"]},
            )
        return sid

    def stop_scenario(self, scenario_id: str) -> bool:
        meta = self.active.get(scenario_id)
        if meta is None:
            return False
        if meta.get("status") == "active":
            meta["status"] = "stopped"
            meta["ended_at"] = self.state.virtual_time
            if self.client is not None:
                self.client.scenario_event(
                    event="stop",
                    scenario_id=scenario_id,
                    machine_id=meta.get("machine_id", ""),
                    scenario_type=meta.get("scenario_type", ""),
                    severity=meta.get("severity"),
                    parts_limit=meta.get("parts_limit_effective"),
                    event_time=self.state.virtual_time,
                )
        return True

    def stop_all(self) -> int:
        ids = [sid for sid, m in self.active.items() if m.get("status") == "active"]
        for sid in ids:
            self.stop_scenario(sid)
        return len(ids)

    def list_active(self) -> list[dict]:
        result = []
        for sid, m in self.active.items():
            if m.get("status") not in ("active", "auto_completed"):
                continue
            machine = self.state.machines.get(m.get("machine_id"))
            processed = 0
            remaining = m.get("parts_limit_effective", m.get("parts_limit"))
            if machine and machine.current_batch_id:
                batch = self.state.batches.get(machine.current_batch_id)
                if batch:
                    processed = sum(1 for s in batch.scenario_marked_indices.values()
                                    if s == sid)
                    remaining = max(0, m.get("parts_limit_effective",
                                             m.get("parts_limit", 0)) - processed)
            result.append({
                "id": sid,
                "machine_id": m.get("machine_id"),
                "scenario_type": m.get("scenario_type"),
                "severity": m.get("severity"),
                "parts_limit": m.get("parts_limit_effective", m.get("parts_limit")),
                "parts_processed": processed,
                "parts_remaining": remaining,
                "status": m.get("status"),
                "started_at": m.get("started_at").isoformat() if m.get("started_at") else None,
            })
        return result

    # ─── вспомогательное ────────────────────────────────────
    def _next_id(self) -> str:
        self._seq += 1
        return f"SC-{self._seq:04d}"

    def _effective_limit(self, machine, parts_limit: int) -> int:
        """Обрезаем parts_limit до остатка текущей партии на станке (если есть)."""
        if machine.machine_type == "furnace":
            return parts_limit  # печь обрабатывает всё одной загрузкой
        bid = machine.current_batch_id
        if not bid:
            return parts_limit
        batch = self.state.batches.get(bid)
        if not batch:
            return parts_limit
        remaining = batch.effective_quantity - machine.parts_done_in_batch
        return max(1, min(parts_limit, remaining))

    def _cleanup_scenario(self, scenario_id: str, now: datetime) -> None:
        """Снятие sensor-модификаторов со станка после auto_completed/stopped.

        ВАЖНО: сам объект сценария НЕ удаляется из state.scenarios_registry —
        партии с помеченными деталями могут дойти до M-GMM ПОЗЖЕ окончания
        сценария (партия в очереди / на следующих этапах / в печи).
        Quality читает scenarios_registry[scenario_id] чтобы сгенерировать
        правильное искажение измерений для каждой помеченной детали.

        Удаляем только из self.active (локального списка контроллера),
        чтобы tick() больше не пытался "доcleanup-ить" уже почищенный сценарий.
        """
        meta = self.active.pop(scenario_id, None)
        if not meta:
            return
        mid = meta.get("machine_id")
        if mid:
            machine = self.state.machines.get(mid)
            if machine:
                for key in meta.get("sensors", {}):
                    machine.anomaly_modifier.pop(key, None)
                if machine.active_scenario_id == scenario_id:
                    machine.active_scenario_id = None
        # Помечаем сценарий как cleaned, чтобы tick больше не трогал его.
        meta["cleaned"] = True

    # ─── списки доступных сценариев для UI ──────────────────
    def catalog(self) -> dict:
        """Описание реестра для UI: machine_type → [scenario_type, ...]."""
        return {
            mt: list(scenarios.keys())
            for mt, scenarios in config.SCENARIOS_BY_MACHINE_TYPE.items()
        }
