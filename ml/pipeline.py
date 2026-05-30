"""Оркестрация ML-пайплайна: Loki → features → ECOD/IForest + Prophet → Postgres.

Веса детекторов ЗАМОРОЖЕНЫ: обучаются явно (train) на чистом baseline и
сохраняются на диск как версия (model_store). Скоринг (run_once / evaluate)
идёт только по уже обученным станкам — без авто-фита на лету. Это даёт
воспроизводимость и снимает переобучение с каждого прогона.
"""
import logging
import threading

import pandas as pd

import config
import features
import forecaster
import loki_client
import model_store
import store
from detectors import MachineDetector

logger = logging.getLogger("ml.pipeline")


class Pipeline:
    def __init__(self):
        self.detectors: dict[str, MachineDetector] = {}
        self.active_version: str | None = None
        self.run_count = 0
        self.last_summary: dict | None = None
        self._lock = threading.Lock()
        self._load_active_on_start()

    def _load_active_on_start(self):
        """Поднять активную версию весов с диска (если есть)."""
        try:
            version = model_store.get_active()
            if version:
                self.detectors = model_store.load_version(version)
                self.active_version = version
                logger.info("active_models_loaded", extra={"details": {
                    "version": version, "machines": sorted(self.detectors)}})
            else:
                logger.info("no_active_models",
                            extra={"details": {"hint": "обучите модель через /train"}})
        except Exception as exc:
            logger.error("active_models_load_failed",
                         extra={"details": {"error": str(exc)}})

    # ─── извлечение ──────────────────────────────────────────
    def _pull_frames(self, real_lookback_min=None):
        records = loki_client.fetch_events(
            config.SENSOR_SERVICE, config.SENSOR_EVENT, real_lookback_min)
        long_df = features.records_to_long(records)
        frames = features.machine_frames(long_df)
        return frames

    @staticmethod
    def _window_bounds(frames):
        lo = hi = None
        for _mid, (_mt, wide) in frames.items():
            if wide.empty:
                continue
            f, t = wide.index.min(), wide.index.max()
            lo = f if lo is None or f < lo else lo
            hi = t if hi is None or t > hi else hi
        to_dt = lambda x: x.to_pydatetime() if x is not None else None
        return to_dt(lo), to_dt(hi)

    # ─── обучение детекторов (→ сохранение версии на диск) ────
    def train(self, real_lookback_min=None, contamination=None,
              tag=None, machine_ids=None) -> dict:
        """Обучает детекторы на текущем окне нормальных данных и сохраняет
        результат как новую ВЕРСИЮ весов на диск, делая её активной.

        contamination — переопределяет долю аномалий для этого обучения.
        machine_ids   — обучить только указанные станки (None = все).
        tag           — человекочитаемая пометка версии (напр. "baseline-month").
        """
        with self._lock:
            frames = self._pull_frames(real_lookback_min)
            want = set(machine_ids) if machine_ids else None
            fitted, skipped = [], []
            new_detectors: dict[str, MachineDetector] = {}
            for mid, (mt, wide) in frames.items():
                if want is not None and mid not in want:
                    continue
                det = MachineDetector(mid, contamination=contamination)
                if det.fit(wide, machine_type=mt):
                    new_detectors[mid] = det
                    fitted.append(mid)
                else:
                    skipped.append(mid)

            wf, wt = self._window_bounds(frames)
            manifest = None
            if new_detectors:
                manifest = model_store.save_version(
                    new_detectors, tag=tag, make_active=True,
                    extra={"window_from": wf.isoformat() if wf else None,
                           "window_to": wt.isoformat() if wt else None,
                           "real_lookback_min": real_lookback_min
                           or config.REAL_LOOKBACK_MIN})
                self.detectors = new_detectors
                self.active_version = manifest["version"]

            run_id = store.new_run("train", wf, wt, {
                "fitted": fitted, "skipped": skipped,
                "version": self.active_version,
                "contamination": contamination or config.CONTAMINATION})
            store.finalize_run(run_id, len(fitted), 0, 0, 0)
            summary = {"run_id": run_id, "fitted": fitted, "skipped": skipped,
                       "machines": len(frames),
                       "version": self.active_version,
                       "saved": manifest is not None}
            logger.info("train_done", extra={"details": summary})
            return summary

    # ─── полный прогон (скоринг по замороженным весам) ───────
    def run_once(self, real_lookback_min=None, do_forecast=None) -> dict:
        with self._lock:
            self.run_count += 1
            if do_forecast is None:
                do_forecast = (self.run_count % config.FORECAST_EVERY_RUNS == 1)

            frames = self._pull_frames(real_lookback_min)
            wf, wt = self._window_bounds(frames)
            run_id = store.new_run("run", wf, wt,
                                   {"do_forecast": bool(do_forecast),
                                    "run_count": self.run_count,
                                    "version": self.active_version})

            n_points = n_anom = n_fc = 0
            detected_per_machine = {}
            untrained = []
            for mid, (mt, wide) in frames.items():
                det = self.detectors.get(mid)
                if det is None or not det.fitted:
                    untrained.append(mid)      # без обученной модели не скорим
                    continue
                scored = det.score(wide)
                if scored is None:
                    continue
                n_points += len(scored)
                store.insert_anomalies(run_id, mid, scored)
                anom = int(scored["is_anomaly"].sum())
                n_anom += anom
                detected_per_machine[mid] = anom

                if do_forecast:
                    for sensor, ser in features.main_series(mt, wide).items():
                        fc = forecaster.forecast_series(ser)
                        n_fc += store.insert_forecasts(run_id, mid, sensor, fc)

            store.finalize_run(run_id, len(detected_per_machine),
                               n_points, n_anom, n_fc)
            summary = {
                "run_id": run_id,
                "scored_machines": len(detected_per_machine),
                "untrained_machines": untrained,
                "points": n_points, "anomalies": n_anom, "forecasts": n_fc,
                "forecast_done": bool(do_forecast),
                "version": self.active_version,
                "window": [wf.isoformat() if wf else None,
                           wt.isoformat() if wt else None],
                "by_machine": detected_per_machine,
            }
            self.last_summary = summary
            logger.info("run_done", extra={"details": summary})
            return summary

    # ─── оценка качества по разметке сценариев ───────────────
    def evaluate(self, real_lookback_min=None) -> dict:
        with self._lock:
            frames = self._pull_frames(real_lookback_min)
            windows = self._scenario_windows(real_lookback_min)

            tol = pd.Timedelta(minutes=config.EVAL_TOLERANCE_MIN)
            tp = fp = fn_points = total_anom_points = total_norm_points = 0
            detected_windows = 0
            total_windows = sum(len(v) for v in windows.values())
            lead_times = []
            untrained = []

            for mid, (_mt, wide) in frames.items():
                det = self.detectors.get(mid)
                if det is None or not det.fitted:
                    untrained.append(mid)
                    continue
                scored = det.score(wide)
                if scored is None:
                    continue
                mwins = windows.get(mid, [])

                def in_window(ts):
                    return any((w0 - tol) <= ts <= (w1 + tol) for w0, w1 in mwins)

                first_det_in_win = {i: None for i in range(len(mwins))}
                for ts, row in scored.iterrows():
                    labeled = in_window(ts)
                    pred = bool(row["is_anomaly"])
                    if labeled:
                        total_anom_points += 1
                    else:
                        total_norm_points += 1
                    if pred and labeled:
                        tp += 1
                    elif pred and not labeled:
                        fp += 1
                    elif (not pred) and labeled:
                        fn_points += 1
                    # lead time: первое обнаружение внутри окна
                    if pred:
                        for i, (w0, w1) in enumerate(mwins):
                            if (w0 - tol) <= ts <= (w1 + tol) and first_det_in_win[i] is None:
                                first_det_in_win[i] = ts

                for i, (w0, w1) in enumerate(mwins):
                    if first_det_in_win[i] is not None:
                        detected_windows += 1
                        # упреждение: от первого детекта до конца окна (≈ alarm/scrap)
                        lead = (w1 - first_det_in_win[i]).total_seconds() / 60.0
                        if lead > 0:
                            lead_times.append(lead)

            precision = tp / (tp + fp) if (tp + fp) else None
            recall_pts = tp / (tp + fn_points) if (tp + fn_points) else None
            f1 = (2 * precision * recall_pts / (precision + recall_pts)
                  if precision and recall_pts else None)
            window_recall = detected_windows / total_windows if total_windows else None
            avg_lead = sum(lead_times) / len(lead_times) if lead_times else None

            result = {
                "version": self.active_version,
                "untrained_machines": untrained,
                "labeled_windows": total_windows,
                "detected_windows": detected_windows,
                "window_recall": _round(window_recall),
                "point_precision": _round(precision),
                "point_recall": _round(recall_pts),
                "point_f1": _round(f1),
                "tp": tp, "fp": fp, "fn": fn_points,
                "anomaly_points_labeled": total_anom_points,
                "normal_points": total_norm_points,
                "avg_lead_time_min": _round(avg_lead),
                "tolerance_min": config.EVAL_TOLERANCE_MIN,
            }
            logger.info("evaluate_done", extra={"details": result})
            return result

    def _scenario_windows(self, real_lookback_min=None) -> dict[str, list]:
        """{machine_id: [(start_dt, end_dt), ...]} из scenario_event-логов.

        start → событие 'start'; end → 'auto_completed'/'stopped' того же
        scenario_id (если конца нет — берём максимум времени окна)."""
        recs = loki_client.fetch_events(
            config.SCENARIO_SERVICE, config.SCENARIO_EVENT, real_lookback_min)
        starts: dict[str, tuple[str, pd.Timestamp]] = {}
        ends: dict[str, pd.Timestamp] = {}
        for r in recs:
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
            if ev == "start":
                starts[sid] = (mid, ts)
            elif ev in ("auto_completed", "stopped"):
                ends[sid] = ts
        windows: dict[str, list] = {}
        for sid, (mid, t0) in starts.items():
            t1 = ends.get(sid, t0 + pd.Timedelta(minutes=config.FORECAST_HORIZON_MIN))
            if t1 < t0:
                t1 = t0
            windows.setdefault(mid, []).append((t0, t1))
        return windows

    # ─── управление версиями весов ───────────────────────────
    def activate_version(self, version: str) -> dict:
        """Загрузить сохранённую версию весов в память и сделать активной."""
        with self._lock:
            detectors = model_store.load_version(version)
            model_store.set_active(version)
            self.detectors = detectors
            self.active_version = version
            return {"active_version": version,
                    "machines": sorted(self.detectors)}

    def list_models(self) -> dict:
        return {"active": model_store.get_active(),
                "versions": model_store.list_versions()}

    def delete_version(self, version: str) -> dict:
        with self._lock:
            ok = model_store.delete_version(version)
            if self.active_version == version:
                # активная удалена — память чистим, новую активную не выбираем
                self.detectors = {}
                self.active_version = None
            return {"deleted": ok, "version": version,
                    "active_version": self.active_version}

    def reset_results(self) -> dict:
        """Чистит результаты в БД и счётчик прогонов. Веса на диске НЕ трогает."""
        with self._lock:
            store.truncate_all()
            self.run_count = 0
            self.last_summary = None
            return {"reset": True, "models_kept": True,
                    "active_version": self.active_version}

    def status(self) -> dict:
        return {
            "active_version": self.active_version,
            "trained_machines": sorted(self.detectors.keys()),
            "detectors": [d.meta() for d in self.detectors.values()],
            "run_count": self.run_count,
            "last_summary": self.last_summary,
            "prophet_available": forecaster.available(),
        }


def _round(x, n=4):
    return round(x, n) if isinstance(x, (int, float)) else x
