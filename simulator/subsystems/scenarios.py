"""ScenariosController: управление сценариями аномалий.

Заглушки и заготовки. Логика инжекции — позже. Сейчас обеспечивается:
- API для установки/снятия anomaly_modifier на станке
- Каркас 5 сценариев (см. документ)
- Хранение активных сценариев

Архитектурный хук: каждый Machine имеет поле anomaly_modifier (dict).
Когда equipment.tick генерирует sensor_reading, значение каждого сенсора
умножается на anomaly_modifier[имя_сенсора] если задано.

Сценарии (на будущее):
1. tool_wear_acceleration — рост vibration_rms_mm_s, spindle_load_percent на hobbing
2. bearing_overheat — рост bearing_temp + vibration_rms_mm_s
3. furnace_drift — дрейф furnace_temp_zoneN
4. coolant_failure — падение coolant_flow, рост coolant_temp_c на grinding
5. quality_burst — повышенный fail rate на work_center
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("simulator.scenarios")


class ScenariosController:
    name = "scenarios"

    def __init__(self, state):
        self.state = state
        # Активные сценарии. Ключ — id сценария, значение — словарь с метаданными.
        self.active: dict[str, dict] = {}
        self._seq = 0

    def reset(self) -> None:
        """Сброс при /restart симулятора."""
        self.active.clear()
        self._seq = 0

    # tick вызывается главным циклом; пока пуст — здесь будет логика
    # постепенного изменения модификаторов со временем.
    def tick(self, now: datetime) -> None:
        # ── TODO: для каждого активного сценария проверить, не пора ли его остановить
        # ── TODO: для сценариев с rampup — постепенно увеличивать модификаторы
        finished = []
        for scenario_id, meta in self.active.items():
            if meta.get("ends_at") is not None and now >= meta["ends_at"]:
                finished.append(scenario_id)
        for sid in finished:
            self.stop_scenario(sid)

    # ─── публичный API сценариев (заглушки) ──────────────────
    def start_tool_wear_acceleration(self, machine_id: str,
                                     duration_min: float,
                                     intensity: float = 1.5) -> str:
        """Сценарий 1: ускоренный износ инструмента на hobbing.
        Растут vibration_rms_mm_s и spindle_load_percent.
        """
        return self._set_modifier(
            machine_id=machine_id,
            scenario_type="tool_wear_acceleration",
            modifiers={
                "vibration_rms_mm_s": intensity,
                "spindle_load_percent": 1.0 + (intensity - 1.0) * 0.5,
            },
            duration_min=duration_min,
        )

    def start_bearing_overheat(self, machine_id: str, duration_min: float,
                               intensity: float = 1.4) -> str:
        """Сценарий 2: перегрев подшипника. Растут bearing_temp + vibration."""
        modifiers = {
            "vibration_rms_mm_s": intensity * 0.8,
        }
        # bearing_temp называется по-разному у разных машин
        machine = self.state.machines.get(machine_id)
        if machine is not None:
            for key in ("spindle_bearing_temp", "hob_bearing_temp", "wheel_bearing_temp"):
                modifiers[key] = intensity
        return self._set_modifier(
            machine_id=machine_id,
            scenario_type="bearing_overheat",
            modifiers=modifiers,
            duration_min=duration_min,
        )

    def start_furnace_drift(self, machine_id: str, zone: int,
                            duration_min: float, drift_pct: float = 0.05) -> str:
        """Сценарий 3: дрейф температуры в одной зоне печи."""
        zone_key = f"furnace_temp_zone{zone}"
        return self._set_modifier(
            machine_id=machine_id,
            scenario_type="furnace_drift",
            modifiers={zone_key: 1.0 + drift_pct},
            duration_min=duration_min,
        )

    def start_coolant_failure(self, machine_id: str, duration_min: float) -> str:
        """Сценарий 4: падение СОЖ на grinding."""
        return self._set_modifier(
            machine_id=machine_id,
            scenario_type="coolant_failure",
            modifiers={
                "coolant_flow": 0.5,    # поток падает в 2 раза
                "coolant_temp_c": 1.4,  # температура растёт
            },
            duration_min=duration_min,
        )

    def start_quality_burst(self, work_center: str, duration_min: float,
                            fail_rate: float = 0.15) -> str:
        """Сценарий 5: вспышка брака на участке. TODO: реализовать."""
        sid = self._next_id()
        self.active[sid] = {
            "type": "quality_burst",
            "work_center": work_center,
            "fail_rate": fail_rate,
            "started_at": self.state.virtual_time,
            "ends_at": self.state.virtual_time + timedelta(minutes=duration_min),
        }
        # TODO: чтобы это работало, QualitySubsystem должна проверять активный
        # quality_burst для work_center и подменять BACKGROUND_FAIL_RATE
        return sid

    def stop_scenario(self, scenario_id: str) -> bool:
        meta = self.active.get(scenario_id)
        if meta is None:
            return False
        machine_id = meta.get("machine_id")
        if machine_id:
            machine = self.state.machines.get(machine_id)
            if machine is not None:
                # снимаем только наши модификаторы (по именам в meta)
                for key in meta.get("modifiers", {}):
                    machine.anomaly_modifier.pop(key, None)
        self.active.pop(scenario_id, None)
        logger.info("stopped scenario %s", scenario_id)
        return True

    def stop_all(self) -> int:
        ids = list(self.active.keys())
        for sid in ids:
            self.stop_scenario(sid)
        return len(ids)

    def list_active(self) -> list[dict]:
        return [{"id": sid, **meta} for sid, meta in self.active.items()]

    # ─── private ─────────────────────────────────────────────
    def _set_modifier(self, machine_id: str, scenario_type: str,
                      modifiers: dict[str, float],
                      duration_min: float) -> str:
        machine = self.state.machines.get(machine_id)
        if machine is None:
            raise ValueError(f"machine {machine_id} not found")
        for key, mult in modifiers.items():
            machine.anomaly_modifier[key] = mult
        sid = self._next_id()
        self.active[sid] = {
            "type": scenario_type,
            "machine_id": machine_id,
            "modifiers": modifiers,
            "started_at": self.state.virtual_time,
            "ends_at": self.state.virtual_time + timedelta(minutes=duration_min),
        }
        logger.info("started scenario %s on %s (%s)",
                    sid, machine_id, scenario_type)
        return sid

    def _next_id(self) -> str:
        self._seq += 1
        return f"SC-{self._seq:04d}"