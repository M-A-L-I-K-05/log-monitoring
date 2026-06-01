"""Детекция аномалий: z-правило по сенсорам (главный триггер) + PyOD
ECOD/IsolationForest (вспомогательные многомерные сигналы).

Модель — на станок (фичи = все сенсоры станка в момент времени). Главный
триггер — максимальное отклонение сенсора от своей обучающей нормы (z): не
разбавляется числом нормальных сенсоров. ECOD/IForest докидывают аномалии-
комбинации. Итог — ИЛИ всех источников (см. score()).
"""
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from pyod.models.ecod import ECOD
from pyod.models.iforest import IForest

import config

logger = logging.getLogger("ml.detectors")


class MachineDetector:
    """ECOD + IForest для одного станка.

    Объект сериализуем целиком (joblib) — ECOD/IForest, нормировка и метаданные
    обучения сохраняются на диск как замороженные веса. См. model_store.py.
    """

    def __init__(self, machine_id: str, contamination: float = None):
        self.machine_id = machine_id
        self.machine_type: str | None = None
        self.contamination = contamination if contamination is not None else config.CONTAMINATION
        self.columns: list[str] | None = None
        self.ecod: ECOD | None = None
        self.iforest: IForest | None = None
        self.train_mean: pd.Series | None = None
        self.train_std: pd.Series | None = None
        # σ ресемплированного (минутного) ряда — норма-полоса для Prophet (он
        # прогнозирует ресемплированный ряд, а не сырые 15с). Детекция её НЕ
        # использует, ей нужна сырая train_std.
        self.train_std_resampled: pd.Series | None = None
        self.n_train = 0
        self.trained_at: str | None = None      # реальное время обучения (ISO)
        self.train_window: list | None = None   # [from, to] виртуального окна

    @property
    def fitted(self) -> bool:
        return self.ecod is not None

    def fit(self, wide: pd.DataFrame, machine_type: str = None) -> bool:
        X = wide.dropna()
        self.columns = list(wide.columns)
        if len(X) < config.MIN_TRAIN_POINTS or X.shape[1] == 0:
            return False
        if machine_type:
            self.machine_type = machine_type
        self.ecod = ECOD(contamination=self.contamination)
        self.ecod.fit(X.values)
        self.iforest = IForest(contamination=self.contamination,
                               random_state=config.IFOREST_RANDOM_STATE)
        self.iforest.fit(X.values)

        if config.THRESHOLD_SIGMA > 0:
            s = self.ecod.decision_scores_
            self.ecod.threshold_ = s.mean() + config.THRESHOLD_SIGMA * s.std()
            s = self.iforest.decision_scores_
            self.iforest.threshold_ = s.mean() + config.THRESHOLD_SIGMA * s.std()
        # сохраняем mean/std обучающего окна для объяснимости (z-вклад сенсора)
        self.train_mean = X.mean()
        self.train_std = X.std(ddof=0).replace(0, np.nan)
        # Минутная σ для Prophet: при усреднении n = бин/интервал независимых
        # показаний (шум симулятора i.i.d.) σ падает в √n раз. Так норма-полоса
        # Prophet меряется в той же шкале, что и ряд, который он прогнозирует.
        _bin = pd.Timedelta(config.RESAMPLE_RULE).total_seconds()
        _n = max(1.0, _bin / config.SENSOR_INTERVAL_SEC)
        self.train_std_resampled = self.train_std / np.sqrt(_n)
        self.n_train = len(X)
        self.trained_at = datetime.now(timezone.utc).isoformat()
        if not X.empty:
            self.train_window = [X.index.min().isoformat(),
                                 X.index.max().isoformat()]
        logger.info("detector_fit", extra={"details": {
            "machine_id": self.machine_id, "n_train": self.n_train,
            "features": self.columns}})
        return True

    def meta(self) -> dict:
        """Сводка для UI/манифеста (без самих весов)."""
        return {
            "machine_id": self.machine_id,
            "machine_type": self.machine_type,
            "fitted": self.fitted,
            "contamination": self.contamination,
            "n_train": self.n_train,
            "features": self.columns or [],
            "trained_at": self.trained_at,
            "train_window": self.train_window,
        }

    # Псевдо-канал персистентности для многомерных детекторов (ECOD/IForest):
    # они не указывают на конкретный сенсор, поэтому в серию идут одним каналом.
    DET_CHANNEL = "__det__"

    def score(self, wide: pd.DataFrame) -> pd.DataFrame | None:
        """Возвращает DataFrame по точкам: score_ecod, score_iforest, метки,
        is_anomaly, top_sensor (сенсор с макс. отклонением по train-z), а также
        колонку hot — список «горячих» каналов в каждой точке (сенсоры за порогом
        + DET_CHANNEL), по которым pipeline считает персистентность по-канально."""
        if not self.fitted:
            return None
        X = wide.reindex(columns=self.columns).ffill().dropna()
        if X.empty:
            return None

        ecod_s = self.ecod.decision_function(X.values)
        ecod_l = self.ecod.predict(X.values)
        if_s = self.iforest.decision_function(X.values)
        if_l = self.iforest.predict(X.values)

        res = pd.DataFrame(index=X.index)
        res["score_ecod"] = ecod_s
        res["label_ecod"] = ecod_l.astype(int)
        res["score_iforest"] = if_s
        res["label_iforest"] = if_l.astype(int)

        # Отклонение каждого сенсора в σ его обучающей нормы (z). Берём МАКСИМУМ
        # по сенсорам, а не сумму — поэтому нормальные сенсоры не «разбавляют»
        # сигнал: один выскочивший сенсор всегда виден, сколько бы нормальных рядом.
        z = (X - self.train_mean) / self.train_std
        z = z.abs().replace([np.inf, -np.inf], np.nan).fillna(0.0)
        res["top_sensor"] = z.idxmax(axis=1)
        res["top_sensor_z"] = z.max(axis=1).round(3)

        # Итог — ИЛИ трёх независимых источников (любой сработал → аномалия):
        #   1) z-правило (главный триггер): любой сенсор вышел за ANOMALY_Z σ.
        #      Ловит дрейф сценариев, не разбавляется числом нормальных сенсоров.
        #   2) ECOD, 3) IForest — многомерные детекторы, докидывают аномалии-
        #      комбинации. any/both — как их сочетать между собой.
        if config.ANOMALY_Z > 0:
            z_flag = res["top_sensor_z"] >= config.ANOMALY_Z
        else:
            z_flag = pd.Series(False, index=res.index)
        if config.ANOMALY_COMBINE == "both":
            det_flag = (res["label_ecod"] == 1) & (res["label_iforest"] == 1)
        else:
            det_flag = (res["label_ecod"] == 1) | (res["label_iforest"] == 1)
        res["is_anomaly"] = z_flag | det_flag

        # «Горячие» каналы каждой точки — для по-канальной персистентности в
        # pipeline. Каждый сенсор за порогом ANOMALY_Z — отдельный канал; ECOD/
        # IForest — один канал DET_CHANNEL. Серия «подряд» считается ОТДЕЛЬНО по
        # каждому каналу, поэтому шум, прыгающий по разным сенсорам, не копит
        # серию, а устойчивый дрейф одного сенсора — копит.
        cols = list(z.columns)
        if config.ANOMALY_Z > 0:
            over = (z >= config.ANOMALY_Z).values
        else:
            over = np.zeros((len(z), len(cols)), dtype=bool)
        det_vals = det_flag.values
        hot = []
        for i in range(len(z)):
            chans = [cols[j] for j in range(len(cols)) if over[i, j]]
            if det_vals[i]:
                chans.append(self.DET_CHANNEL)
            hot.append(chans)
        res["hot"] = hot
        return res
