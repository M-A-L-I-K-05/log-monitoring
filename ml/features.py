"""Подготовка фичей: JSON-записи sensor_reading → pandas → ряды по станкам.

Ось времени — ВИРТУАЛЬНОЕ event_time из JSON (не реальное время Loki).
"""
import logging

import pandas as pd

import config

logger = logging.getLogger("ml.features")


def records_to_long(records: list[dict]) -> pd.DataFrame:
    """Плоская таблица: одна строка = одно показание одного сенсора.

    Колонки: event_time, machine_id, machine_type, product_code, sensor, value.
    """
    rows = []
    for r in records:
        et = r.get("event_time")
        ent = r.get("entity_id")
        details = r.get("details") or {}
        readings = details.get("readings") or {}
        mtype = details.get("machine_type")
        product_code = details.get("product_code")
        if not et or not ent or not readings:
            continue
        for sensor, val in readings.items():
            if isinstance(val, (int, float)):
                rows.append((et, ent, mtype, product_code, sensor, float(val)))

    df = pd.DataFrame(
        rows, columns=["event_time", "machine_id", "machine_type",
                        "product_code", "sensor", "value"])
    if df.empty:
        return df
    df["event_time"] = pd.to_datetime(df["event_time"], errors="coerce")
    df = df.dropna(subset=["event_time"])
    return df.sort_values("event_time")


_CARBURIZING_CP_THRESHOLD = 0.7   # carbon_potential выше этого → carburizing, ниже → heating


def filter_furnace_phase(wide: pd.DataFrame, phase: str) -> pd.DataFrame:
    """Оставляет в wide_df только строки, относящиеся к фазе печи.

    Нужно для Prophet: запрос тянет показания печи целиком (carburizing +
    quenching + heating), а прогноз строим по одной фазе — иначе ряд смешает
    разные физические режимы.
    """
    if wide.empty:
        return wide
    if phase == "quenching":
        if "quench_oil_temp" not in wide.columns:
            return wide.iloc[0:0]
        return wide[wide["quench_oil_temp"].notna()]
    if phase == "carburizing":
        if "carbon_potential" not in wide.columns:
            return wide.iloc[0:0]
        return wide[wide["carbon_potential"] > _CARBURIZING_CP_THRESHOLD]
    return wide.iloc[0:0]


def _keep_context(mtype: str, pcode) -> bool:
    """Проверяет, нужно ли обрабатывать комбинацию (machine_type, product_code/phase).

    Для печи — только фазы из FURNACE_ML_PHASES.
    Для остальных станков — product_code должен быть задан.
    """
    if mtype in config.SKIP_MACHINE_TYPES:
        return False
    if mtype == "furnace":
        return pcode in config.FURNACE_ML_PHASES
    return pcode is not None and not (isinstance(pcode, float) and pd.isna(pcode))


def machine_frames(long_df: pd.DataFrame,
                   resample: str = None) -> dict[tuple, tuple]:
    """{(machine_id, context): (machine_type, wide_df)} — для скоринга.

    context = product_code для обычных станков, фаза для печи (определяется по сенсорам).
    """
    resample = resample or config.RESAMPLE_RULE
    frames: dict[tuple, tuple] = {}
    if long_df.empty:
        return frames

    for mid, g in long_df.groupby("machine_id"):
        mtype = g["machine_type"].iloc[0]
        if mtype in config.SKIP_MACHINE_TYPES:
            continue

        if mtype == "furnace":
            # В одном кадре печи смешаны обе фазы (product_code=null). Делим СТРОКИ
            # по фазе до ресемпла: carbon_potential>0.7 → carburizing,
            # quench_oil_temp → quenching. dropna(axis=1) убирает мёртвые сенсоры
            # другой фазы, иначе detector.fit/score теряет все строки.
            raw = g.pivot_table(index="event_time", columns="sensor",
                                values="value", aggfunc="mean").sort_index()
            for phase in config.FURNACE_ML_PHASES:
                pw = filter_furnace_phase(raw, phase)
                if pw.empty:
                    continue
                pw = pw.resample(resample).mean().dropna(axis=1, how="all")
                pw = pw.ffill().dropna(how="all")
                if not pw.empty:
                    frames[(mid, phase)] = (mtype, pw)
        else:
            for pcode, pg in g.groupby("product_code", dropna=True):
                if pd.isna(pcode) or pcode is None:
                    continue
                wide = pg.pivot_table(index="event_time", columns="sensor",
                                      values="value", aggfunc="mean").sort_index()
                wide = wide.resample(resample).mean().ffill().dropna(how="all")
                if wide.empty:
                    continue
                frames[(mid, pcode)] = (mtype, wide)
    return frames


def type_product_frames(long_df: pd.DataFrame,
                        resample: str = None,
                        max_points: int = None) -> dict[tuple, tuple]:
    """{(machine_type, context): (n_points, wide_df)} — для обучения.

    Для обычных станков context = product_code.
    Для печи context = фаза (carburizing / quenching), определяется из сенсоров.
    """
    resample = resample or config.RESAMPLE_RULE
    max_points = max_points or config.TRAIN_POINTS
    frames: dict[tuple, tuple] = {}
    if long_df.empty:
        return frames

    for mtype, mg in long_df.groupby("machine_type"):
        if mtype in config.SKIP_MACHINE_TYPES:
            continue

        if mtype == "furnace":
            # Печь обучаем по фазе. ВАЖНО пивотить ПОМАШИННО: при общем pivot по
            # event_time сенсоры разных печей (одна в carburizing, другая в
            # quenching в тот же момент) сольются в одну строку, и в кадр фазы
            # попадут чужие колонки (carburizing получит quench_*). Тогда
            # model.columns ≠ колонкам при скоринге (там помашинно) → score()
            # теряет все строки. Помашинный сплит даёт ровно сенсоры своей фазы;
            # ряды разных печей пулим конкатенацией строк по общим колонкам.
            parts: dict[str, list] = {ph: [] for ph in config.FURNACE_ML_PHASES}
            for _mid, mmg in mg.groupby("machine_id"):
                raw = mmg.pivot_table(index="event_time", columns="sensor",
                                      values="value", aggfunc="mean").sort_index()
                for phase in config.FURNACE_ML_PHASES:
                    pw = filter_furnace_phase(raw, phase)
                    if pw.empty:
                        continue
                    pw = pw.resample(resample).mean().dropna(axis=1, how="all")
                    pw = pw.ffill().dropna(how="all")
                    if not pw.empty:
                        parts[phase].append(pw)
            for phase, plist in parts.items():
                if not plist:
                    continue
                common = [c for c in plist[0].columns
                          if all(c in p.columns for p in plist)]
                wide = pd.concat([p[common] for p in plist]).sort_index()
                if len(wide) > max_points:
                    wide = wide.iloc[-max_points:]
                frames[(mtype, phase)] = (len(wide), wide)
        else:
            for pcode, pg in mg.groupby("product_code", dropna=True):
                if pd.isna(pcode) or pcode is None:
                    continue
                wide = pg.pivot_table(index="event_time", columns="sensor",
                                      values="value", aggfunc="mean").sort_index()
                wide = wide.resample(resample).mean().ffill().dropna(how="all")
                if wide.empty:
                    continue
                if len(wide) > max_points:
                    wide = wide.iloc[-max_points:]
                frames[(mtype, pcode)] = (len(wide), wide)
    return frames


def main_series(mtype: str, wide: pd.DataFrame,
                product_code: str = None) -> dict[str, pd.Series]:
    """Главные ряды для Prophet.

    Поиск сенсоров: сначала конкретный ключ "mtype__product_code",
    затем общий ключ "mtype". Это позволяет задавать разные сенсоры
    для каждой комбинации (тип станка, тип шестерни/фаза) в MAIN_SENSORS.
    """
    specific_key = f"{mtype}__{product_code}" if product_code else None
    wanted = (
        config.MAIN_SENSORS.get(specific_key)
        or config.MAIN_SENSORS.get(mtype, [])
    )
    out = {}
    for s in wanted:
        if s in wide.columns:
            ser = wide[s].dropna()
            if not ser.empty:
                out[s] = ser
    return out
