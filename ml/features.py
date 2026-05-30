"""Подготовка фичей: JSON-записи sensor_reading → pandas → ряды по станкам.

Ось времени — ВИРТУАЛЬНОЕ event_time из JSON (не реальное время Loki).
"""
import logging

import pandas as pd

import config

logger = logging.getLogger("ml.features")


def records_to_long(records: list[dict]) -> pd.DataFrame:
    """Плоская таблица: одна строка = одно показание одного сенсора.

    Колонки: event_time, machine_id, machine_type, sensor, value.
    """
    rows = []
    for r in records:
        et = r.get("event_time")
        ent = r.get("entity_id")
        details = r.get("details") or {}
        readings = details.get("readings") or {}
        mtype = details.get("machine_type")
        if not et or not ent or not readings:
            continue
        for sensor, val in readings.items():
            if isinstance(val, (int, float)):
                rows.append((et, ent, mtype, sensor, float(val)))

    df = pd.DataFrame(
        rows, columns=["event_time", "machine_id", "machine_type", "sensor", "value"])
    if df.empty:
        return df
    df["event_time"] = pd.to_datetime(df["event_time"], errors="coerce")
    df = df.dropna(subset=["event_time"])
    # Симулятор может перезапускаться (virtual_time сбрасывается) → возможны
    # «прыжки» назад. Берём только последний непрерывный сегмент: всё, что не
    # старше максимального event_time на разумное виртуальное окно.
    return df.sort_values("event_time")


def machine_frames(long_df: pd.DataFrame,
                   resample: str = None) -> dict[str, tuple[str, pd.DataFrame]]:
    """{machine_id: (machine_type, wide_df)} — wide: index=время, cols=сенсоры.

    Ресемпл на RESAMPLE_RULE (среднее), forward-fill пропусков.
    Станки SKIP_MACHINE_TYPES (инспекция) пропускаются.
    """
    resample = resample or config.RESAMPLE_RULE
    frames: dict[str, tuple[str, pd.DataFrame]] = {}
    if long_df.empty:
        return frames

    for mid, g in long_df.groupby("machine_id"):
        mtype = g["machine_type"].iloc[0]
        if mtype in config.SKIP_MACHINE_TYPES:
            continue
        wide = g.pivot_table(index="event_time", columns="sensor",
                             values="value", aggfunc="mean").sort_index()
        # дубликаты индекса убираем агрегированием pivot_table; ресемпл + ffill
        wide = wide.resample(resample).mean().ffill().dropna(how="all")
        if wide.empty:
            continue
        frames[mid] = (mtype, wide)
    return frames


def main_series(mtype: str, wide: pd.DataFrame) -> dict[str, pd.Series]:
    """Главные ряды станка для Prophet (только сенсоры из MAIN_SENSORS[mtype])."""
    wanted = config.MAIN_SENSORS.get(mtype, [])
    out = {}
    for s in wanted:
        if s in wide.columns:
            ser = wide[s].dropna()
            if not ser.empty:
                out[s] = ser
    return out
