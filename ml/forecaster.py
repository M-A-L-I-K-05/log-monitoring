"""Прогноз тренда сенсора (Prophet) для predictive maintenance.

Сезонность отключена (завод 24/7, синтетика — Prophet иначе «выдумает» циклы).
interval_width=0.99 → широкий доверительный интервал; выход реального значения
за интервал = ранний предиктивный сигнал (ещё ДО alarm).
"""
import logging

import pandas as pd

import config

logger = logging.getLogger("ml.forecaster")
# Prophet/cmdstanpy очень болтливы — глушим.
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.ERROR)

try:
    from prophet import Prophet
    _PROPHET_OK = True
except Exception as exc:   # pragma: no cover
    logger.error("prophet_import_failed", extra={"details": {"error": str(exc)}})
    _PROPHET_OK = False


def available() -> bool:
    return _PROPHET_OK


def forecast_series(series: pd.Series,
                    horizon_min: int = None) -> pd.DataFrame | None:
    """Обучает Prophet на ряду и возвращает прогноз in-sample + на горизонт.

    Колонки результата: ts, yhat, yhat_lower, yhat_upper, actual, breach.
    breach=True, если реальное значение вышло за доверительный интервал.
    """
    if not _PROPHET_OK:
        return None
    horizon_min = horizon_min if horizon_min is not None else config.FORECAST_HORIZON_MIN
    s = series.dropna()
    if len(s) < config.MIN_TRAIN_POINTS:
        return None

    dfp = pd.DataFrame({"ds": s.index, "y": s.values})
    try:
        m = Prophet(weekly_seasonality=False, yearly_seasonality=False,
                    daily_seasonality=False,
                    interval_width=config.PROPHET_INTERVAL_WIDTH)
        m.fit(dfp)
        future = m.make_future_dataframe(periods=horizon_min, freq="min")
        fc = m.predict(future)
    except Exception as exc:
        logger.error("prophet_fit_failed", extra={"details": {"error": str(exc)}})
        return None

    out = fc[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy().set_index("ds")
    out["actual"] = dfp.set_index("ds")["y"]
    out["breach"] = out["actual"].notna() & (
        (out["actual"] < out["yhat_lower"]) | (out["actual"] > out["yhat_upper"]))
    out = out.reset_index().rename(columns={"ds": "ts"})
    return out
