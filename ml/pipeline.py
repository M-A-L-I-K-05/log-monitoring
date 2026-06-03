"""Оркестрация ML-пайплайна: Loki → features → ECOD/IForest + Prophet → Postgres.

Модели строятся по (machine_type, product_code), а не по machine_id —
сенсорные данные зависят от типа станка и типа шестерни, не от конкретной машины.

Обучение: явное (train), на последних TRAIN_FETCH_LIMIT записях из Loki.
Скоринг: инкрементальный (run_once), окно = SCORING_LOOKBACK_MIN реальных минут.
"""
import logging
import threading
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

import config
import features
import forecaster
import loki_client
import model_store
import store
from detectors import MachineDetector

logger = logging.getLogger("ml.pipeline")

_DET_KEY_SEP = "__"


def _det_key(machine_type: str, product_code: str) -> str:
    return f"{machine_type}{_DET_KEY_SEP}{product_code}"


class Pipeline:
    def __init__(self):
        self.detectors: dict[str, MachineDetector] = {}
        self.active_version: str | None = None
        self.run_count = 0
        self.last_summary: dict | None = None
        # Последнее записанное виртуальное время по (machine_id, product_code) —
        # для дедупа: окна скоринга перекрываются, пишем только точки новее.
        self._last_seen: dict[tuple, pd.Timestamp] = {}
        # Счётчик персистентности по (machine_id, product_code): для КАЖДОГО
        # «горячего» канала (сенсор или ECOD/IForest) — сколько точек подряд он
        # горяч. {key: {channel: count}}. Переживает батчи, поэтому серия
        # считается через границы прогонов.
        self._persist_count: dict[tuple, dict[str, int]] = {}
        # Реальное время конца прошлого успешного фетча скоринга — чтобы окно
        # «догоняло» пачку логов после перемотки (+N мин) или паузы симулятора,
        # а не ограничивалось фиксированными ~10с. Дедуп по virtual event_time
        # ниже всё равно не даст записать одну точку дважды.
        self._last_fetch_end: datetime | None = None
        # Предыдущее состояние прогнозной аномалии по (machine_id, sensor) — чтобы
        # писать в ml_prophet_events только ФРОНТ нарастания (false→true), а не
        # каждый цикл, пока аномалия держится.
        self._prophet_prev: dict[tuple, bool] = {}
        self._lock = threading.Lock()
        self._load_active_on_start()

    def _load_active_on_start(self):
        try:
            version = model_store.get_active()
            if version:
                self.detectors = model_store.load_version(version)
                self.active_version = version
                logger.info("active_models_loaded", extra={"details": {
                    "version": version, "keys": sorted(self.detectors)}})
            else:
                logger.info("no_active_models",
                            extra={"details": {"hint": "обучите модель через /train"}})
        except Exception as exc:
            logger.error("active_models_load_failed",
                         extra={"details": {"error": str(exc)}})

    # ─── обучение ────────────────────────────────────────────────
    def train(self, contamination=None, tag=None) -> dict:
        """Обучает детекторы по (machine_type, product_code).

        Запрашивает последние TRAIN_FETCH_LIMIT записей из Loki.
        Каждая комбинация должна иметь ≥ TRAIN_POINTS точек — иначе пропускается
        с сообщением пользователю.
        """
        with self._lock:
            records = loki_client.fetch_for_training()
            long_df = features.records_to_long(records)
            tp_frames = features.type_product_frames(long_df)

            trained, skipped, insufficient = [], [], []
            new_detectors: dict[str, MachineDetector] = {}

            for (mtype, pcode), (n_points, wide) in tp_frames.items():
                key = _det_key(mtype, pcode)
                if n_points < config.TRAIN_POINTS:
                    insufficient.append({
                        "key": key, "points": n_points,
                        "needed": config.TRAIN_POINTS})
                    skipped.append(key)
                    continue
                det = MachineDetector(key, contamination=contamination)
                if det.fit(wide, machine_type=mtype):
                    new_detectors[key] = det
                    trained.append({"key": key, "points": n_points})
                else:
                    skipped.append(key)

            manifest = None
            if new_detectors:
                manifest = model_store.save_version(
                    new_detectors, tag=tag, make_active=True,
                    extra={"contamination": contamination or config.CONTAMINATION,
                           "train_points_required": config.TRAIN_POINTS})
                self.detectors = new_detectors
                self.active_version = manifest["version"]

            run_id = store.new_run("train", None, None, {
                "trained": [t["key"] for t in trained],
                "skipped": skipped,
                "version": self.active_version})
            store.finalize_run(run_id, len(trained), 0, 0, 0)

            summary = {
                "run_id": run_id,
                "trained": trained,
                "skipped": skipped,
                "insufficient": insufficient,
                "version": self.active_version,
                "saved": manifest is not None,
            }
            if insufficient:
                summary["message"] = (
                    "Недостаточно данных для некоторых комбинаций. "
                    f"Нужно ≥ {config.TRAIN_POINTS} точек. "
                    "Прогоните симулятор ещё немного и повторите обучение."
                )
            logger.info("train_done", extra={"details": summary})
            return summary

    # ─── скоринг ─────────────────────────────────────────────────
    def run_once(self, do_forecast=None, lookback_min=None) -> dict:
        """Инкрементальный скоринг за последние lookback_min реальных минут.

        lookback_min задаёт фоновый поток как (интервал + запас); при ручном
        вызове None → fetch_events берёт дефолт config.SCORING_LOOKBACK_MIN.
        """
        with self._lock:
            self.run_count += 1
            if do_forecast is None:
                do_forecast = (self.run_count % config.FORECAST_EVERY_RUNS == 1)

            records = loki_client.fetch_events(
                config.SENSOR_SERVICE, config.SENSOR_EVENT,
                self._effective_lookback(lookback_min))
            long_df = features.records_to_long(records)
            frames = features.machine_frames(long_df)

            wf, wt = self._window_bounds(frames)
            run_id = store.new_run("run", wf, wt,
                                   {"do_forecast": bool(do_forecast),
                                    "run_count": self.run_count,
                                    "version": self.active_version})

            n_points = n_anom = n_fc = 0
            detected_per_key = {}
            untrained = []

            for (mid, pcode), (mtype, wide) in frames.items():
                key = _det_key(mtype, pcode) if pcode else None
                det = self.detectors.get(key) if key else None
                if det is None or not det.fitted:
                    untrained.append(f"{mid}({mtype}/{pcode})")
                    continue

                scored = det.score(wide)
                if scored is None:
                    continue

                # Дедуп по виртуальному времени: окна скоринга перекрываются,
                # поэтому пишем только точки новее последней записанной — иначе
                # одни и те же аномалии добавлялись бы в БД каждый прогон.
                last = self._last_seen.get((mid, pcode))
                if last is not None:
                    scored = scored[scored.index > last]
                if scored.empty:
                    continue
                self._last_seen[(mid, pcode)] = scored.index.max()

                # Персистентность: кандидат → аномалия только после PERSIST_N подряд.
                scored = self._apply_persistence((mid, pcode), scored)

                n_points += len(scored)
                store.insert_anomalies(run_id, mid, scored,
                                       machine_type=mtype, product_code=pcode)
                anom = int(scored["is_anomaly"].sum())
                n_anom += anom
                detected_per_key[f"{mid}/{pcode}"] = anom

                if do_forecast and pcode:
                    n_fc += self._run_prophet(run_id, mid, mtype, pcode)

            store.finalize_run(run_id, len(detected_per_key),
                               n_points, n_anom, n_fc)
            summary = {
                "run_id": run_id,
                "scored": len(detected_per_key),
                "untrained": untrained,
                "points": n_points,
                "anomalies": n_anom,
                "forecasts": n_fc,
                "forecast_done": bool(do_forecast),
                "version": self.active_version,
                "by_machine": detected_per_key,
            }
            self.last_summary = summary
            logger.info("run_done", extra={"details": summary})
            return summary

    def _effective_lookback(self, lookback_min) -> float:
        """Окно fetch_events с «догоном»: не уже базового, но растягивается назад
        до прошлого успешного фетча. Так пачка логов после перемотки (+N мин) или
        после паузы попадёт в окно при любом тайминге тика. Дедуп по virtual
        event_time не даст записать одну точку дважды. Ограничено пределом длины
        запроса Loki."""
        base = lookback_min if lookback_min is not None else config.SCORING_LOOKBACK_MIN
        now_wall = datetime.now(timezone.utc)
        eff = base
        if self._last_fetch_end is not None:
            gap_min = (now_wall - self._last_fetch_end).total_seconds() / 60.0
            eff = max(base, gap_min + config.SCORING_MARGIN_SEC / 60.0)
        eff = min(eff, config.LOKI_MAX_QUERY_DAYS * 24 * 60)
        self._last_fetch_end = now_wall
        return eff

    def _apply_persistence(self, key: tuple, scored: pd.DataFrame) -> pd.DataFrame:
        """SPC run-rule ПО-КАНАЛЬНО: аномалия, когда ХОТЯ БЫ ОДИН «горячий» канал
        (сенсор за порогом или ECOD/IForest) держится PERSIST_N точек ПОДРЯД.

        Считаем серию отдельно по каждому каналу: горяч в этой точке → +1, иначе
        канал сбрасывается в 0. Так шум, перепрыгивающий между разными сенсорами
        (каждый раз вылетает другой из 6–8), не копит серию — а устойчивый дрейф
        одного сенсора копит. Счётчик по key переживает батчи (серия считается
        через границы прогонов). scored отсортирован по виртуальному времени.
        """
        n = config.PERSIST_N
        if n <= 1:
            return scored
        counts = self._persist_count.get(key, {})
        flags = []
        for hot in scored["hot"].tolist():
            # инкрементируем только горячие каналы; отсутствующие = сброс в 0
            counts = {ch: counts.get(ch, 0) + 1 for ch in hot}
            flags.append(any(c >= n for c in counts.values()))
        self._persist_count[key] = counts
        scored["is_anomaly"] = flags
        return scored

    # ─── прогноз (predictive) ────────────────────────────────────
    def _run_prophet(self, run_id: int, mid: str, mtype: str, pcode: str) -> int:
        """Предиктивная аналитика по (machine_id, product_code/фаза).

        Тянет СОБСТВЕННОЕ окно из Loki — последние PROPHET_FETCH_POINTS показаний
        этой машины с нужной шестернёй (для печи — нужной фазы), независимо от
        скорингового окна: Prophet нужен длинный ряд. Один запрос на станок,
        дальше прогноз по каждому главному сенсору → ml_forecasts.
        breach = выход реального значения за доверительный интервал = ранний
        предиктивный сигнал (ещё до alarm).
        """
        if not forecaster.available():
            return 0
        # для печи product_code в логах пустой — фильтруем по фазе уже после pivot
        pc_filter = None if mtype == "furnace" else pcode
        records = loki_client.fetch_for_machine(
            mid, pc_filter, limit=config.PROPHET_FETCH_POINTS)
        long_df = features.records_to_long(records)
        if long_df.empty:
            return 0
        wide = long_df.pivot_table(index="event_time", columns="sensor",
                                   values="value", aggfunc="mean").sort_index()
        if mtype == "furnace":
            wide = features.filter_furnace_phase(wide, pcode)
        wide = features.to_continuous_minutes(wide, max_points=config.PROPHET_SERIES_POINTS)
        if wide.empty:
            return 0

        n = 0
        for sensor, ser in features.main_series(
                mtype, wide, product_code=pcode).items():
            fc = forecaster.forecast_series(ser)
            if fc is None:
                continue
            n += store.insert_forecasts(run_id, mid, sensor, fc)
        return n

    # ─── предиктивный контур Prophet (отдельный от детекции) ─────
    def run_prophet_cycle(self, lookback_min=None) -> dict:
        """Прогнозный цикл: для активных станков предсказываем главные сенсоры на
        горизонт и проверяем, не уйдёт ли прогноз за норму.

        1) по свежему окну находим активные станки и их текущий контекст
           (тип шестерни / фаза печи);
        2) для каждого станка × главный сенсор обучаем Prophet на своём длинном
           окне (PROPHET_FETCH_POINTS) и берём прогноз на FORECAST_HORIZON_MIN;
        3) аномалия = yhat на горизонте выходит за норму сенсора (train_mean ±
           ANOMALY_Z·σ из ОБУЧЕННОГО детектора этой комбинации — модели не меняем,
           только читаем их норму);
        4) пишем витрину ml_prophet_status для карточек Grafana.

        Контур независим от детекции (run_once) — детекторы и их веса не трогаются.
        """
        if not forecaster.available():
            return {"prophet": "unavailable"}
        # Fetch'и — ВНЕ лока (чтение Loki + чистая обработка), чтобы не держать общий
        # лок и не тормозить детекцию. Активный контекст (текущая фаза печи / шестерня)
        # берём по свежему малому окну; длинную ИСТОРИЮ под прогноз — ОДНИМ лёгким
        # bulk-запросом (fetch_recent, без `| json`), режем по (станок, контекст) в
        # Python. Так нет 12 тяжёлых per-station запросов, что и вешало фоновый поток.
        active = set(features.machine_frames(features.records_to_long(
            loki_client.fetch_events(
                config.SENSOR_SERVICE, config.SENSOR_EVENT, lookback_min))).keys())
        pframes = features.prophet_frames(
            features.records_to_long(loki_client.fetch_recent(config.PROPHET_BULK_FETCH)),
            config.PROPHET_SERIES_POINTS)

        with self._lock:
            rows = []
            machines = anomalies = 0
            checked, no_norm = [], []
            for (mid, ctx) in active:
                pf = pframes.get((mid, ctx))
                if pf is None:                 # нет истории этого контекста в bulk
                    continue
                mtype, wide = pf
                det = self.detectors.get(_det_key(mtype, ctx))
                if det is None or not det.fitted:
                    no_norm.append(f"{mid}({mtype}/{ctx})")
                    continue
                sensors = self._prophet_sensors(mtype, ctx, det)
                if not sensors:
                    continue

                machines += 1
                for sensor in sensors:
                    row = self._prophet_check_sensor(mid, mtype, ctx, det, wide, sensor)
                    if row is None:
                        continue
                    rows.append(row[:11])          # витрина-снимок (11 полей)
                    checked.append(f"{mid}/{sensor}")
                    is_anom = row[4]
                    if is_anom:
                        anomalies += 1
                    # Лог-история: пишем только ФРОНТ нарастания (false→true), чтобы
                    # один эпизод дал одну строку, а не по строке на каждый цикл.
                    pkey = (mid, sensor)
                    if is_anom and not self._prophet_prev.get(pkey, False):
                        detected_at = row[11]      # виртуальное «сейчас» прогноза
                        store.insert_prophet_event(
                            mid, mtype, ctx, sensor, detected_at,
                            lead_min=row[7], n_breaches=row[5], horizon_points=row[6],
                            norm_lower=row[8], norm_upper=row[9], yhat_end=row[10])
                    self._prophet_prev[pkey] = bool(is_anom)

            store.upsert_prophet_status(rows)
            # Чистим застрявшие строки (станок встал / сменил фазу печи), чтобы
            # карточки Grafana не показывали статус из прошлого.
            pruned = store.prune_prophet_status(config.PROPHET_STATUS_TTL_SEC)
            summary = {"prophet_cycle": True, "machines": machines,
                       "sensors_checked": len(rows), "anomalies": anomalies,
                       "pruned": pruned, "no_norm": no_norm, "checked": checked}
            logger.info("prophet_cycle_done", extra={"details": summary})
            return summary

    def _prophet_sensors(self, mtype: str, ctx: str, det) -> list[str]:
        """Главные сенсоры комбинации, по которым есть норма в детекторе."""
        key = f"{mtype}__{ctx}"
        wanted = config.MAIN_SENSORS.get(key) or config.MAIN_SENSORS.get(mtype, [])
        cols = set(det.columns or [])
        return [s for s in wanted if s in cols]

    def _prophet_band_std(self, det, sensor, raw_std):
        """σ для норма-полосы Prophet в МИНУТНОЙ шкале (как у ряда, что он
        прогнозирует). Берём сохранённую при обучении train_std_resampled; если
        её нет (старая модель) — пересчитываем из сырой σ: при усреднении
        n = бин/интервал независимых показаний σ падает в √n раз."""
        rs = getattr(det, "train_std_resampled", None)
        if rs is not None and sensor in rs.index:
            val = float(rs[sensor])
            if np.isfinite(val) and val > 0:
                return val
        bin_sec = pd.Timedelta(config.RESAMPLE_RULE).total_seconds()
        n = max(1.0, bin_sec / config.SENSOR_INTERVAL_SEC)
        return raw_std / np.sqrt(n)

    def _prophet_check_sensor(self, mid, mtype, ctx, det, wide, sensor):
        """Прогноз одного сенсора + проверка выхода за норму. Возвращает кортеж
        для store.upsert_prophet_status или None, если прогноз не построить."""
        if sensor not in wide.columns:
            return None
        ser = wide[sensor].dropna()
        # Отдельный, более высокий минимум для Prophet: на коротком ряду он
        # экстраполирует шум за полосу (ложные карточки на старте фазы). См.
        # config.PROPHET_MIN_POINTS.
        if len(ser) < config.PROPHET_MIN_POINTS:
            return None
        fc = forecaster.forecast_series(ser, config.FORECAST_HORIZON_MIN)
        if fc is None:
            return None
        try:
            mean = float(det.train_mean[sensor])
            std = float(det.train_std[sensor])
        except (KeyError, TypeError, ValueError):
            return None
        if not np.isfinite(std) or std <= 0:
            return None

        # Полосу меряем МИНУТНОЙ σ: Prophet прогнозирует ресемплированный (минутный)
        # ряд, а детекторная train_std — сырая 15с (вдвое «громче»). С сырой σ полоса
        # была бы вдвое шире, и прогноз почти никогда её не пробивал бы. Берём
        # сохранённую при обучении train_std_resampled; для старых моделей без неё —
        # пересчитываем из сырой σ по тому же √n.
        band_std = self._prophet_band_std(det, sensor, std)
        if band_std is None or not np.isfinite(band_std) or band_std <= 0:
            return None

        lower = mean - config.ANOMALY_Z * band_std
        upper = mean + config.ANOMALY_Z * band_std
        future = fc[fc["actual"].isna()]      # только горизонт (без in-sample)
        if future.empty:
            return None
        over = (future["yhat"] < lower) | (future["yhat"] > upper)
        n_breaches = int(over.sum())
        is_anom = n_breaches > 0
        # now_v — виртуальное «сейчас» прогноза (последняя фактическая точка ряда);
        # нужно и для lead, и как detected_at в логе ml_prophet_events.
        now_v = fc.loc[fc["actual"].notna(), "ts"].max()
        lead_min = None
        if is_anom:
            first_ts = future.loc[over, "ts"].min()
            lead_min = round((first_ts - now_v).total_seconds() / 60.0, 1)
        return (mid, mtype, ctx, sensor, is_anom, n_breaches, int(len(future)),
                lead_min, round(lower, 4), round(upper, 4),
                round(float(future["yhat"].iloc[-1]), 4),
                now_v.to_pydatetime() if hasattr(now_v, "to_pydatetime") else now_v)

    # ─── оценка качества ─────────────────────────────────────────
    def evaluate(self, real_lookback_min=None) -> dict:
        """Количественная оценка по размеченным сценариям.

        Источник правды — окна сценариев (ml_scenarios, синхронизируются из логов
        scenario_event инкрементально от курсора). Срабатывания берём из УЖЕ
        записанных таблиц, а не пересчётом из Loki: детектор — ml_anomalies,
        Prophet — ml_prophet_events. Так оценка не упирается в потолок выгрузки Loki
        и сводится к сравнению «окно сценария ↔ пойманные аномалии». Возвращает
        раздельные секции detector/prophet + разбор по каждому сценарию (корреляция
        по названию сценария).
        """
        with self._lock:
            # Привязка к ТЕКУЩЕМУ прогону: окно по виртуальному времени данных
            # детекции/прогноза. Сценарии из прошлых прогонов (остались в Loki/БД,
            # id переиспользуются) отсекаются — иначе ловим призраков и коллизии.
            run_span = store.get_run_span()
            if run_span[0] is None:
                return {"version": self.active_version, "scenarios": 0,
                        "note": "нет данных детекции — обучи модель и прогони сценарии"}
            tol = timedelta(minutes=config.EVAL_TOLERANCE_MIN)
            horizon = timedelta(minutes=config.FORECAST_HORIZON_MIN)
            margin = tol + horizon
            run_lo, run_hi = run_span[0] - margin, run_span[1] + margin
            synced = self._sync_scenarios((run_lo, run_hi))
            scenarios = store.get_scenarios(run_lo, run_hi)
            if not scenarios:
                return {"version": self.active_version, "scenarios": 0,
                        "note": "в окне текущего прогона нет размеченных сценариев",
                        "synced": synced}

            wins, span_from, span_to = [], None, None
            for s in scenarios:
                w0 = s["started_at"]
                # Конец окна: событие завершения, если оно есть. Если сценарий открыт
                # (в логах только start) — тянем окно до конца данных прогона. На станке
                # в один момент активен только один сценарий, поэтому любое срабатывание
                # на этой машине после старта относится именно к нему — в том числе
                # предиктивное предупреждение Prophet ещё ДО того, как дрейф достиг
                # порога (корректная ранняя поимка, а не ложное срабатывание).
                w1 = s["ended_at"] or max(run_span[1], w0 + horizon)
                if w1 < w0:
                    w1 = w0
                wins.append({"id": s["scenario_id"], "type": s["scenario_type"],
                             "machine_id": s["machine_id"], "sensors": s["sensors"],
                             "w0": w0, "w1": w1})
                lo, hi = w0 - tol, w1 + tol
                span_from = lo if span_from is None else min(span_from, lo)
                span_to = hi if span_to is None else max(span_to, hi)

            det_anoms = store.get_detector_anomalies(span_from, span_to)
            pr_events = store.get_prophet_events(span_from, span_to)

            def _inside_any(mid, ts):
                return any(w["machine_id"] == mid
                           and (w["w0"] - tol) <= ts <= (w["w1"] + tol) for w in wins)

            # ── корреляция по каждому сценарию ──
            by_scenario = []
            det_caught = pr_caught = 0
            det_latencies, pr_leads, pr_latencies = [], [], []
            for w in wins:
                m, w0, w1 = w["machine_id"], w["w0"], w["w1"]
                d_times = sorted(et for (mid, et, _s, _z) in det_anoms
                                 if mid == m and (w0 - tol) <= et <= (w1 + tol))
                d_ok = bool(d_times)
                d_lat = round((d_times[0] - w0).total_seconds() / 60.0, 1) if d_ok else None
                if d_ok:
                    det_caught += 1
                    det_latencies.append(d_lat)

                p_hits = sorted(((dt, lead) for (mid, _se, dt, lead, _b) in pr_events
                                 if mid == m and (w0 - tol) <= dt <= (w1 + tol)),
                                key=lambda x: x[0])
                p_ok = bool(p_hits)
                p_lead = p_hits[0][1] if p_ok else None
                # Задержка Prophet — по аналогии с детектором: через сколько минут
                # ПОСЛЕ начала сценария Prophet впервые предупредил (detected_at - w0).
                # Дополняет упреждение (lead): lead меряет «через сколько прогноз
                # пробьёт границу», задержка — «через сколько после старта среагировал».
                p_lat = round((p_hits[0][0] - w0).total_seconds() / 60.0, 1) if p_ok else None
                if p_ok:
                    pr_caught += 1
                    if p_lead is not None:
                        pr_leads.append(p_lead)
                    if p_lat is not None:
                        pr_latencies.append(p_lat)

                by_scenario.append({
                    "scenario_id": w["id"], "scenario": w["type"],
                    "machine_id": m, "sensors": w["sensors"],
                    "window_start": w0.isoformat(), "window_end": w1.isoformat(),
                    "detector": {"caught": d_ok, "detect_latency_min": d_lat},
                    "prophet": {"caught": p_ok, "lead_min": p_lead,
                                "detect_latency_min": p_lat},
                })

            total = len(wins)
            det_tp = sum(1 for (mid, et, _s, _z) in det_anoms if _inside_any(mid, et))
            det_fp = len(det_anoms) - det_tp
            pr_tp = sum(1 for (mid, _se, dt, _l, _b) in pr_events if _inside_any(mid, dt))
            pr_fp = len(pr_events) - pr_tp

            result = {
                "version": self.active_version,
                "scenarios": total,
                "tolerance_min": config.EVAL_TOLERANCE_MIN,
                "run_window": [run_span[0].isoformat(), run_span[1].isoformat()],
                "synced_events": synced,
                "detector": {
                    "scenarios_detected": det_caught,
                    "recall": _round(det_caught / total if total else None),
                    "precision": _round(det_tp / (det_tp + det_fp) if (det_tp + det_fp) else None),
                    "tp_points": det_tp, "fp_points": det_fp,
                    "avg_detect_latency_min": _round(
                        sum(det_latencies) / len(det_latencies) if det_latencies else None),
                },
                "prophet": {
                    "scenarios_detected": pr_caught,
                    "recall": _round(pr_caught / total if total else None),
                    "precision": _round(pr_tp / (pr_tp + pr_fp) if (pr_tp + pr_fp) else None),
                    "tp_events": pr_tp, "fp_events": pr_fp,
                    "avg_lead_min": _round(
                        sum(pr_leads) / len(pr_leads) if pr_leads else None),
                    "avg_detect_latency_min": _round(
                        sum(pr_latencies) / len(pr_latencies) if pr_latencies else None),
                },
                "by_scenario": by_scenario,
            }
            logger.info("evaluate_done", extra={"details": {
                k: v for k, v in result.items() if k != "by_scenario"}})
            return result

    def _sync_scenarios(self, run_span) -> int:
        """Читает логи scenario_event из Loki и пишет окна ТЕКУЩЕГО прогона в
        ml_scenarios. Логи сценариев лёгкие (2 на сценарий), поэтому берём широкое
        окно, но пишем только события, чьё ВИРТУАЛЬНОЕ время попадает в диапазон
        текущих данных детекции (run_span). Так окна из прошлых прогонов, оставшиеся
        в Loki, не попадают в оценку и не коллизят по scenario_id (id переиспользуются
        каждый прогон). Возвращает число обработанных событий старт/стоп."""
        span_lo, span_hi = run_span
        if span_lo is None:
            return 0
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=config.LOKI_MAX_QUERY_DAYS)
        logql = '{service_name="%s", event="%s"}' % (
            config.SCENARIO_SERVICE, config.SCENARIO_EVENT)
        records = loki_client.query_range(logql, start, end)
        n = 0
        for r in records:
            sid = r.get("entity_id")
            et = r.get("event_time")
            d = r.get("details") or {}
            ev = d.get("event")
            mid = d.get("machine_id")
            if not sid or not et or not mid:
                continue
            ts = pd.to_datetime(et, errors="coerce")
            if pd.isna(ts):
                continue
            ts = ts.to_pydatetime()
            if not (span_lo <= ts <= span_hi):   # сценарий из другого прогона
                continue
            if ev == "start":
                sensors = (d.get("extra") or {}).get("sensors")
                if isinstance(sensors, dict):
                    sensors_s = ",".join(sensors.keys())      # {сенсор: величина} → имена
                elif isinstance(sensors, list):
                    sensors_s = ",".join(map(str, sensors))
                else:
                    sensors_s = str(sensors) if sensors else None
                store.upsert_scenario_start(sid, d.get("scenario_type"), mid,
                                            sensors_s, d.get("severity"), ts)
                n += 1
            elif ev in ("auto_completed", "stopped"):
                store.upsert_scenario_end(sid, ts)
                n += 1
        return n

    # ─── управление версиями ─────────────────────────────────────
    def activate_version(self, version: str) -> dict:
        with self._lock:
            detectors = model_store.load_version(version)
            model_store.set_active(version)
            self.detectors = detectors
            self.active_version = version
            return {"active_version": version, "keys": sorted(self.detectors)}

    def list_models(self) -> dict:
        return {"active": model_store.get_active(),
                "versions": model_store.list_versions()}

    def delete_version(self, version: str) -> dict:
        with self._lock:
            ok = model_store.delete_version(version)
            if self.active_version == version:
                self.detectors = {}
                self.active_version = None
            return {"deleted": ok, "version": version,
                    "active_version": self.active_version}

    def delete_all_versions(self) -> dict:
        with self._lock:
            n = model_store.delete_all_versions()
            self.detectors = {}
            self.active_version = None
            self._last_seen.clear()
            return {"deleted_versions": n, "active_version": None}

    def reset_results(self) -> dict:
        with self._lock:
            store.truncate_all()
            self.run_count = 0
            self.last_summary = None
            self._last_seen.clear()   # иначе после очистки БД дедуп пропустит перескоринг
            self._persist_count.clear()
            self._last_fetch_end = None
            self._prophet_prev.clear()  # сбросить фронт Prophet — иначе после reset
            #                             пропустит первое предупреждение по сенсору
            return {"reset": True, "models_kept": True,
                    "active_version": self.active_version}

    def status(self) -> dict:
        return {
            "active_version": self.active_version,
            "trained_keys": sorted(self.detectors.keys()),
            "detectors": [d.meta() for d in self.detectors.values()],
            "run_count": self.run_count,
            "last_summary": self.last_summary,
            "prophet_available": forecaster.available(),
            "scoring_lookback_min": config.SCORING_LOOKBACK_MIN,
            "train_points_required": config.TRAIN_POINTS,
        }

    @staticmethod
    def _window_bounds(frames):
        lo = hi = None
        for (_mid, _pcode), (_mt, wide) in frames.items():
            if wide.empty:
                continue
            f, t = wide.index.min(), wide.index.max()
            lo = f if lo is None or f < lo else lo
            hi = t if hi is None or t > hi else hi
        to_dt = lambda x: x.to_pydatetime() if x is not None else None
        return to_dt(lo), to_dt(hi)


def _round(x, n=4):
    return round(x, n) if isinstance(x, (int, float)) else x
