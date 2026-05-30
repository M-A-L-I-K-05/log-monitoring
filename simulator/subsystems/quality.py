"""QualitySubsystem: контроль качества на M-GMM после каждого этапа.

Базовый режим (без активного сценария):
  - Промежуточный этап: 1 случайная деталь по параметрам этапа (Таблица A).
    Если фоновый fail — 2 соседних детали force_pass.
  - Финал (inspection): 10%-выборка всей партии, все 8 параметров.

Режим со сценарием:
  - Поголовно меряются все детали с пометкой scenario_marked_indices →
    каждая fail с причиной сценария, искажённые сенсоры/измерения.
  - Остальные (годные) → 10%-выборка → все pass (+ возможный фон).

Фоновый брак:
  - BACKGROUND_FAIL_RATE = 0.003 на этап (≈1.5% за маршрут).
  - reason="background_random", source_machine_id=NULL, scenario_id=NULL.
  - Сенсоры НЕ искажаются.

Equipment-подсистема ставит задачу через state.pending_measurements:
  (batch_id, stage_after, machine_id, event_time) — где stage_after — этап,
  после которого партия отправлена на M-GMM (turning|hobbing|...|inspection).
"""
import logging
import random
from datetime import datetime

import config

logger = logging.getLogger("simulator.quality")

INSPECTOR_ID = "M-GMM"


class QualitySubsystem:
    name = "quality"

    def __init__(self, state, client):
        self.state = state
        self.client = client

    def tick(self, now: datetime) -> None:
        """Измерение управляется EquipmentSubsystem поштучно: M-GMM меряет
        деталь за деталью по виртуальному времени и вызывает measure_plan_item
        напрямую. Здесь Quality по таймеру ничего не делает."""
        return

    # ─── построение плана измерения партии ────────────────────
    def build_plan(self, batch, stage: str, gmm_id: str) -> list[dict]:
        """Упорядоченный список деталей к измерению на M-GMM. Элемент:
        {idx, mode, scenario_id, source, force_pass}.

        Режимы:
          scenario — поголовно все помеченные сценарием (всегда fail),
          final    — 10% выборка годных на финальной инспекции,
          verify   — 10% выборка годного остатка партии, захваченной сценарием,
          spot     — 1 случайная годная на промежуточном этапе. Соседние
                     (spot_neighbor) добавляются ДИНАМИЧЕСКИ в equipment,
                     если spot-деталь забракована.
        """
        all_indices = set(range(1, batch.quantity + 1)) - batch.failed_indices
        scenario_indices = sorted(k for k in batch.scenario_marked_indices.keys()
                                  if k in all_indices)
        clean_indices = sorted(all_indices - set(scenario_indices))
        is_final = (stage == "inspection")
        last_proc_id = batch.last_processed_machine_id

        plan: list[dict] = []
        for idx in scenario_indices:
            plan.append({"idx": idx, "mode": "scenario",
                         "scenario_id": batch.scenario_marked_indices[idx],
                         "source": last_proc_id, "force_pass": False})

        if clean_indices:
            if is_final or scenario_indices:
                n_sample = max(1, int(round(len(clean_indices) * config.INSPECTION_SAMPLE_RATIO)))
                sample = sorted(random.sample(clean_indices, min(n_sample, len(clean_indices))))
                mode = "final" if is_final else "verify"
                for idx in sample:
                    plan.append({"idx": idx, "mode": mode, "scenario_id": None,
                                 "source": None, "force_pass": False})
            else:
                spot = random.choice(clean_indices)
                plan.append({"idx": spot, "mode": "spot", "scenario_id": None,
                             "source": None, "force_pass": False})
        return plan

    def spot_neighbors(self, batch, spot_idx: int) -> list[dict]:
        """Соседние детали к забракованной spot-детали (доизмерение, force_pass).
        Вызывается equipment'ом, когда spot-деталь оказалась браком — план
        измерения вырастает на эти детали (+2 по умолчанию)."""
        all_indices = set(range(1, batch.quantity + 1)) - batch.failed_indices
        out: list[dict] = []
        for off in range(1, config.SPOT_CHECK_NEIGHBORS_ON_FAIL + 1):
            for cand in (spot_idx + off, spot_idx - off):
                if cand in all_indices and cand != spot_idx:
                    out.append({"idx": cand, "mode": "spot_neighbor",
                                "scenario_id": None, "source": None,
                                "force_pass": True})
                    break
        return out

    def measure_plan_item(self, batch, item: dict, stage: str,
                          gmm_id: str, now: datetime) -> str:
        """Измерить одну деталь из плана. Возвращает 'pass'/'fail'."""
        return self._measure_part(
            batch, item["idx"], stage, gmm_id, now,
            mode=item["mode"], scenario_id=item["scenario_id"],
            source_machine_id=item["source"], force_pass=item["force_pass"])

    # ─── измерение одной детали по N параметрам этапа ─────────
    def _measure_part(self, batch, part_idx: int, stage: str,
                      gmm_id: str, now: datetime,
                      mode: str,
                      scenario_id: str | None,
                      source_machine_id: str | None,
                      force_pass: bool = False) -> str:
        """Меряет одну деталь по всем параметрам этапа.
        Возвращает "pass"/"fail" — общее решение по детали.

        mode:
          "scenario"      — поголовное измерение под сценарием (всегда fail
                            по тем параметрам, что в сценарии),
          "spot"          — 1 случайная деталь на промежуточном этапе,
          "spot_neighbor" — соседняя при force_pass=True,
          "final"         — 10%-выборка на финальной инспекции,
          "verify"        — 10%-выборка годного остатка партии под сценарием.
        """
        part_id = f"P-{batch.batch_id}-{part_idx:04d}"
        params = config.STAGE_MEASUREMENTS.get(stage, [])
        specs = config.MEASUREMENT_SPECS_BY_PRODUCT.get(
            batch.product_code, config.MEASUREMENT_SPECS_BY_PRODUCT["SPUR-M"]
        )

        # ── решаем, кто этой детали добавит fail ──
        # Эти три источника взаимоисключающие на одной детали:
        # 1) scenario — сценарий уверенно бракует свои параметры,
        # 2) background — фоновый брак (только если НЕ scenario),
        # 3) force_pass / spot_neighbor — никогда не fail.
        fail_params: dict[str, str] = {}  # param → "up"|"down"
        reason: str | None = None

        if mode == "scenario" and scenario_id:
            scenario = self._lookup_scenario(scenario_id)
            if scenario:
                measurements_dir = scenario.get("measurements", {})
                severity = scenario.get("severity", config.DEFAULT_SEVERITY)
                # бракуются ТОЛЬКО те параметры, что есть в сценарии И в этапе
                for p, direction in measurements_dir.items():
                    if p in params:
                        fail_params[p] = direction
                reason = scenario.get("scenario_type")
                if not fail_params:
                    # сценарий не задевает параметры этого этапа — всё pass
                    pass
        elif not force_pass:
            # фон: ОДИН случайный параметр этапа
            if params and random.random() < config.BACKGROUND_FAIL_RATE:
                victim = random.choice(params)
                direction = config.PARAM_FAIL_DIRECTION.get(victim, "up")
                if direction == "both":
                    direction = random.choice(["up", "down"])
                fail_params[victim] = direction
                reason = "background_random"

        any_fail = bool(fail_params)
        severity = config.DEFAULT_SEVERITY
        if mode == "scenario" and scenario_id:
            scenario = self._lookup_scenario(scenario_id)
            if scenario:
                severity = scenario.get("severity", config.DEFAULT_SEVERITY)

        for param in params:
            spec = specs.get(param)
            if spec is None:
                continue
            value, nominal, tolerance, unit, is_fail_value = self._gen_value(
                spec, param, fail_params, severity, reason=reason
            )
            self.client.measurement(
                batch_id=batch.batch_id,
                part_id=part_id,
                part_index=part_idx,
                product_code=batch.product_code,
                stage=stage,
                machine_id=source_machine_id or gmm_id,
                work_center=stage,
                parameter=param,
                value=round(value, 4),
                nominal=round(nominal, 4),
                tolerance=round(tolerance, 4),
                unit=unit,
                result=("fail" if is_fail_value else "pass"),
                reason=(reason if is_fail_value else None),
                source_machine_id=(source_machine_id if is_fail_value and reason != "background_random" else None),
                scenario_id=(scenario_id if is_fail_value and mode == "scenario" else None),
                event_time=now,
            )

        decision = "fail" if any_fail else "pass"

        # Учёт. parts_fail — каждая забракованная шестерня (выбывает из партии,
        # считается один раз). parts_pass здесь НЕ инкрементим: годные шестерни
        # засчитываются при завершении партии (переход в done) — иначе в pass
        # попадали бы только измеренные единицы, а не все прошедшие маршрут.
        if decision == "fail":
            batch.fails_count += 1
            self.state.counters["parts_fail"] += 1
            batch.failed_indices.add(part_idx)
            if reason:
                batch.defects_by_reason[reason] = batch.defects_by_reason.get(reason, 0) + 1

        self.client.inspection_result(
            part_id=part_id,
            batch_id=batch.batch_id,
            work_center=stage,
            decision=decision,
            event_time=now,
            inspector_id=gmm_id,
            reason=reason if decision == "fail" else None,
        )
        return decision

    # ─── вспомогательное ──────────────────────────────────────
    def _lookup_scenario(self, scenario_id: str) -> dict | None:
        # сценарии хранятся в ScenariosController; быстрее всего — через state.
        sc = getattr(self.state, "scenarios_registry", None)
        if sc is not None:
            return sc.get(scenario_id)
        return None

    def _gen_value(self, spec, param: str,
                   fail_params: dict[str, str],
                   severity: str,
                   reason: str | None = None) -> tuple[float, float, float, str, bool]:
        """Генерит value, nominal, tolerance, unit, is_fail_value для одного параметра.

        Spec — кортеж: ("deviation", nominal, tolerance, unit) или
                       ("range", min, max, unit).
        Excess (выход за границу допуска):
          - фоновый брак — всегда light, BACKGROUND_FAIL_EXCESS (0.05–0.25),
          - сценарный — масштабируется по severity сценария.
        """
        kind = spec[0]
        if reason == "background_random":
            excess_lo, excess_hi = config.BACKGROUND_FAIL_EXCESS
        else:
            sev = config.SEVERITY_LEVELS.get(severity, config.SEVERITY_LEVELS[config.DEFAULT_SEVERITY])
            excess_lo, excess_hi = sev["measure_excess"]
        if param not in fail_params:
            excess_lo, excess_hi = (0.0, 0.0)

        if kind == "deviation":
            _, nominal, tolerance, unit = spec
            lo, hi = nominal, nominal + tolerance
            if param in fail_params:
                direction = fail_params[param]
                excess = random.uniform(excess_lo, excess_hi)
                if direction == "up":
                    value = hi + excess * tolerance
                elif direction == "down":
                    # deviation измеряется как модуль, "down" редко используется,
                    # но трактуем как "ниже nominal" (хотя физически модуль не уйдёт ниже).
                    # Для совместимости: трактуем как up.
                    value = hi + excess * tolerance
                else:
                    value = hi + excess * tolerance
                is_fail = True
            else:
                # норма: 10–95% от tolerance над nominal
                value = random.uniform(nominal + 0.10 * tolerance, nominal + 0.95 * tolerance)
                is_fail = False
            tol_for_log = tolerance / 2.0  # historic style: ± tolerance/2 around mid
            nominal_for_log = nominal + tolerance / 2.0
            return value, nominal_for_log, tol_for_log, unit, is_fail

        elif kind == "range":
            _, lo, hi, unit = spec
            mid = (lo + hi) / 2.0
            half = (hi - lo) / 2.0
            if param in fail_params:
                direction = fail_params[param]
                excess = random.uniform(excess_lo, excess_hi)
                if direction == "up":
                    value = hi + excess * (hi - lo)
                elif direction == "down":
                    value = lo - excess * (hi - lo)
                else:
                    value = hi + excess * (hi - lo) if random.random() < 0.5 else lo - excess * (hi - lo)
                is_fail = True
            else:
                # норма: внутри [lo+20%*range, hi-20%*range]
                inner = (hi - lo) * 0.2
                value = random.uniform(lo + inner, hi - inner)
                is_fail = False
            return value, mid, half, unit, is_fail

        else:
            raise ValueError(f"unknown spec kind: {kind}")
