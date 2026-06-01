"""ScenariosController: управление сценариями аномалий по реестру config.

Режимы (config.SCENARIOS_BY_MACHINE_TYPE[type][name]["mode"]):
- gradual: медленный дрейф; drift_progress накапливается по деталям в
  equipment._tick_running_processing. Фаза 1 (0 → scrap_threshold): сенсоры
  ползут, брака нет, пересекает границы партий. Фаза 2 (scrap → stop):
  детали тегаются; сценарий завершается по stop_threshold ИЛИ концу партии.
- step: полная аномалия с первой детали; завершается по parts_cap (из конфига
  по severity ± jitter).

Самозавершение — equipment._auto_complete_scenario: alarm → WO (pending_scenario_wos)
→ станок idle. ScenariosController.tick() снимает модификаторы (_cleanup_scenario),
сбрасывает drift_progress.
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
        # снять все модификаторы и drift_progress со станков
        for sid, meta in list(self.active.items()):
            mid = meta.get("machine_id")
            if mid:
                machine = self.state.machines.get(mid)
                if machine:
                    for key in meta.get("sensors", {}):
                        machine.anomaly_modifier.pop(key, None)
                    machine.active_scenario_id = None
                    machine.drift_progress = 0.0
        # сброс drift_progress на всех станках (на случай если сценарий не в active)
        for machine in self.state.machines.values():
            machine.drift_progress = 0.0
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

        self._check_pause_on_start()

        if self._auto_enabled:
            self._maybe_auto_start(now)

    # ─── Авто-пауза при старте сценария ──────────────────────
    def _check_pause_on_start(self) -> None:
        """Если у активного сценария взведён pause_on_start — ставим симулятор
        на паузу в момент, когда деформация реально начинается.

        Печь: деформация включается phase-lock'ом только в trigger_phase
        (carburizing/quenching), поэтому ждём, пока загрузка войдёт в эту фазу.
        Остальные станки: деформация идёт, как только станок в running.
        """
        for meta in self.active.values():
            if not meta.get("pause_on_start") or meta.get("_pause_done"):
                continue
            if meta.get("status") != "active":
                continue
            mid = meta.get("machine_id")
            machine = self.state.machines.get(mid)
            trigger = meta.get("trigger_phase")
            if trigger:
                load = self.state.furnace_loads.get(mid)
                deforming = load is not None and load.phase == trigger
            else:
                deforming = machine is not None and machine.state == "running"
            if deforming:
                self.state.pause()
                meta["_pause_done"] = True
                logger.info("auto-paused simulator on scenario %s start (%s on %s)",
                            meta.get("id"), meta.get("scenario_type"), mid)

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
                       parts_limit: int = 30,
                       pause_on_start: bool = False) -> str:
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

        sev = config.SEVERITY_LEVELS[severity]
        sensor_scale = sev["sensor_scale"]

        mode = spec.get("mode", "step")
        is_furnace = (machine.machine_type == "furnace")

        # Целевые множители:
        # gradual (печь и не-печь): target напрямую из конфига — откалиброван
        #   по 3σ (при drift=scrap_threshold значение сенсора = mean ± 3σ).
        #   sensor_scale НЕ применяется: severity влияет только на темп дрейфа
        #   (для печи — на число загрузок до отбраковки, см. furnace.py).
        # step: масштабируем sensor_scale по severity.
        sensors_final: dict[str, float] = {}
        for k, mult in spec["sensors"].items():
            if mode == "gradual":
                sensors_final[k] = mult
            else:
                sensors_final[k] = 1.0 + (mult - 1.0) * sensor_scale

        if mode == "gradual":
            # Дрейф начинается с 0 — anomaly_modifier пока не действует.
            # Для печи дрейф растёт по времени в carburizing (furnace.py),
            # pace в деталях не используется.
            machine.drift_progress = 0.0
            for k in sensors_final:
                machine.anomaly_modifier[k] = 1.0
            scrap_threshold = config.DRIFT_SCRAP_THRESHOLD
            stop_threshold = config.DRIFT_STOP_THRESHOLD
            pace = None if is_furnace else config.DRIFT_PACE_BY_SEVERITY[severity]
            parts_cap = None
        else:
            # step: аномалия полная с первой детали / с первого тика.
            # Для печи модификатор включается phase-lock'ом только в quenching
            # (furnace.py), завершение управляется временем, а не parts_cap.
            machine.drift_progress = 1.0
            for k, v in sensors_final.items():
                machine.anomaly_modifier[k] = v
            scrap_threshold = 0.0
            stop_threshold = None
            pace = None
            if is_furnace:
                parts_cap = None
            else:
                jitter = random.randint(-config.STEP_PARTS_CAP_JITTER,
                                        config.STEP_PARTS_CAP_JITTER)
                parts_cap = config.STEP_PARTS_CAP.get(severity, 10) + jitter

        # parts_limit_effective — для совместимости с API и scenario_event-логом.
        parts_limit_effective = self._effective_limit(machine, parts_limit, mode, parts_cap)

        sid = self._next_id()
        meta = {
            "id": sid,
            "machine_id": machine_id,
            "machine_type": machine.machine_type,
            "scenario_type": scenario_type,
            "severity": severity,
            "mode": mode,
            "sensors_final": sensors_final,
            "sensors": sensors_final,          # backward compat (_cleanup_scenario)
            "measurements": dict(spec.get("measurements", {})),
            "wo_duration_min": spec.get("wo_duration_min", 30.0),
            "trigger_phase": spec.get("trigger_phase"),
            "scrap_threshold": scrap_threshold,
            "stop_threshold": stop_threshold,
            "pace": pace,
            "parts_cap": parts_cap,
            "parts_limit": parts_limit,
            "parts_limit_effective": parts_limit_effective,
            "started_at": self.state.virtual_time,
            "status": "active",
            # Авто-пауза: когда деформация реально начнётся (для печи — вход в
            # trigger-фазу, для остальных — станок в running), цикл встаёт на паузу.
            "pause_on_start": bool(pause_on_start),
            "_pause_done": False,
        }
        self.active[sid] = meta
        self.state.scenarios_registry[sid] = meta
        machine.active_scenario_id = sid

        logger.info("started scenario %s on %s (%s, mode=%s, sev=%s, cap=%s)",
                    sid, machine_id, scenario_type, mode, severity,
                    parts_cap if parts_cap is not None else "gradual")
        if self.client is not None:
            self.client.scenario_event(
                event="start",
                scenario_id=sid,
                machine_id=machine_id,
                scenario_type=scenario_type,
                severity=severity,
                parts_limit=parts_limit_effective,
                event_time=self.state.virtual_time,
                details={"mode": mode,
                         "sensors": sensors_final,
                         "measurements": meta["measurements"],
                         "wo_duration_min": meta["wo_duration_min"],
                         "scrap_threshold": scrap_threshold,
                         "stop_threshold": stop_threshold,
                         "parts_cap": parts_cap},
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
            mode = m.get("mode", "step")
            machine_type = m.get("machine_type")
            is_furnace = machine_type == "furnace"
            processed = self._count_tagged(machine, sid)
            drift_p = round(machine.drift_progress, 4) if machine else None

            # Для печи step «прогресс» — по времени фазы (см. furnace.py),
            # а не по деталям. Считаем прошедшее время в trigger-фазе и фазу.
            elapsed_min = None
            threshold_min = None
            furnace_phase = None
            if is_furnace and mode == "step":
                threshold_min = config.FURNACE_STEP_CATCH_MIN
                elapsed_min, furnace_phase = self._furnace_step_progress(m)

            # Для step (не печь): показываем оставшийся cap; для gradual — дрейф.
            if mode == "step" and not is_furnace:
                cap = m.get("parts_cap", m.get("parts_limit_effective", m.get("parts_limit")))
                parts_remaining = max(0, cap - processed) if cap else None
            else:
                parts_remaining = None
            result.append({
                "id": sid,
                "machine_id": m.get("machine_id"),
                "machine_type": machine_type,
                "scenario_type": m.get("scenario_type"),
                "severity": m.get("severity"),
                "mode": mode,
                "drift_progress": drift_p,
                "scrap_threshold": m.get("scrap_threshold"),
                "stop_threshold": m.get("stop_threshold"),
                "parts_cap": m.get("parts_cap"),
                "parts_tagged": processed,
                "parts_remaining": parts_remaining,
                "elapsed_min": elapsed_min,
                "threshold_min": threshold_min,
                "furnace_phase": furnace_phase,
                "status": m.get("status"),
                "started_at": m.get("started_at").isoformat() if m.get("started_at") else None,
            })
        return result

    def _count_tagged(self, machine, sid: str) -> int:
        """Сколько деталей помечено данным сценарием.

        Обычный станок — по текущей партии; печь — по всем партиям загрузки.
        """
        if machine is None:
            return 0
        if machine.machine_type == "furnace":
            load = self.state.furnace_loads.get(machine.machine_id)
            batch_ids = load.batch_ids if load else \
                self.state.frozen_furnace_batches.get(machine.machine_id, [])
            total = 0
            for bid in batch_ids:
                b = self.state.batches.get(bid)
                if b:
                    total += sum(1 for s in b.scenario_marked_indices.values() if s == sid)
            return total
        if machine.current_batch_id:
            batch = self.state.batches.get(machine.current_batch_id)
            if batch:
                return sum(1 for s in batch.scenario_marked_indices.values() if s == sid)
        return 0

    def _furnace_step_progress(self, meta: dict) -> tuple[float, str]:
        """(прошедшие минуты в trigger-фазе, фаза normal|scrap) для печного step."""
        machine = self.state.machines.get(meta.get("machine_id"))
        load = self.state.furnace_loads.get(meta.get("machine_id")) if machine else None
        trigger = meta.get("trigger_phase")
        if load is None or trigger is None or load.phase != trigger \
                or load.phase_started_at is None:
            # ещё не вошли в trigger-фазу (или уже вышли) → 0 мин, normal
            return 0.0, "normal"
        elapsed = (self.state.virtual_time - load.phase_started_at).total_seconds() / 60.0
        elapsed = max(0.0, round(elapsed, 1))
        phase = "scrap" if elapsed >= config.FURNACE_STEP_CATCH_MIN else "normal"
        return elapsed, phase

    # ─── вспомогательное ────────────────────────────────────
    def _next_id(self) -> str:
        self._seq += 1
        return f"SC-{self._seq:04d}"

    def _effective_limit(self, machine, parts_limit: int,
                         mode: str = "step", parts_cap: int | None = None) -> int:
        """Информационный лимит для scenario_event-лога и list_active().

        gradual: не привязываем к одной партии (фаза 1 пересекает границы).
                 Возвращаем 0 как маркер «не применимо» — реальная остановка
                 управляется stop_threshold / концом партии в фазе 2.
        step:    возвращаем parts_cap (уже посчитан с jitter).
        furnace: возвращаем исходный parts_limit (batch-ориентированный).
        """
        if machine.machine_type == "furnace":
            return parts_limit
        if mode == "gradual":
            return 0   # нет фиксированного лимита в деталях
        # step
        return parts_cap if parts_cap is not None else parts_limit

    def _cleanup_scenario(self, scenario_id: str, now: datetime) -> None:
        """Снятие sensor-модификаторов и сброс drift_progress после auto_completed/stopped.

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
                machine.drift_progress = 0.0
        # Помечаем сценарий как cleaned, чтобы tick больше не трогал его.
        meta["cleaned"] = True

    # ─── списки доступных сценариев для UI ──────────────────
    def catalog(self) -> dict:
        """Описание реестра для UI: machine_type → [{type, mode}, ...]."""
        return {
            mt: [
                {"type": name, "mode": spec.get("mode", "step")}
                for name, spec in scenarios.items()
            ]
            for mt, scenarios in config.SCENARIOS_BY_MACHINE_TYPE.items()
        }
