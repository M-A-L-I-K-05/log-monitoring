"""Клиент Loki: выгрузка structured-JSON логов через query_range.

Loki индексирует по реальному времени приёма, поэтому окно [start, end] задаём
в РЕАЛЬНОМ времени, а виртуальное event_time достаём из самого JSON (см.
features). Пагинация — назад по времени (direction=backward) постранично, пока
не упрёмся в начало окна или в потолок MAX_LOG_LINES.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import requests

import config

logger = logging.getLogger("ml.loki")


def _ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


def query_range(logql: str, start: datetime, end: datetime,
                limit: int = None, max_lines: int = None) -> list[dict]:
    """Возвращает список JSON-записей (распарсенных dict) за реальное окно."""
    limit = limit or config.LOKI_PAGE_LIMIT
    max_lines = max_lines or config.MAX_LOG_LINES
    url = f"{config.LOKI_URL}/loki/api/v1/query_range"

    collected: list[tuple[int, str]] = []
    cur_end = end
    while True:
        params = {
            "query": logql,
            "start": _ns(start),
            "end": _ns(cur_end),
            "limit": limit,
            "direction": "backward",
        }
        try:
            r = requests.get(url, params=params, timeout=config.LOKI_TIMEOUT_SEC)
            r.raise_for_status()
        except Exception as exc:
            logger.error("loki_query_failed", extra={"details": {
                "error": str(exc), "logql": logql}})
            break

        result = r.json().get("data", {}).get("result", [])
        page: list[tuple[int, str]] = []
        for stream in result:
            for ns, line in stream.get("values", []):
                page.append((int(ns), line))
        if not page:
            break

        page.sort(key=lambda x: x[0], reverse=True)   # newest first
        collected.extend(page)
        if len(collected) >= max_lines:
            collected = collected[:max_lines]
            logger.warning("loki_truncated", extra={"details": {
                "max_lines": max_lines, "logql": logql}})
            break

        oldest_ns = page[-1][0]
        new_end = datetime.fromtimestamp(
            oldest_ns / 1e9, tz=timezone.utc) - timedelta(microseconds=1)
        # Если страница неполная или дошли до начала окна — выходим.
        if len(page) < limit or new_end <= start:
            break
        cur_end = new_end

    records = []
    for _ns_ts, line in collected:
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
    return records


def fetch_events(service: str, event: str,
                 real_lookback_min: float = None) -> list[dict]:
    """Тянет записи конкретного события за недавнее реальное окно (для скоринга)."""
    real_lookback_min = real_lookback_min if real_lookback_min is not None \
        else config.SCORING_LOOKBACK_MIN
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=real_lookback_min)
    logql = '{service_name="%s", event="%s"}' % (service, event)
    records = query_range(logql, start, end)
    logger.info("loki_fetch", extra={"details": {
        "service": service, "event": event,
        "real_lookback_min": real_lookback_min, "records": len(records)}})
    return records


def fetch_for_machine(machine_id: str, product_code: str,
                      limit: int = None) -> list[dict]:
    """Последние N sensor_reading для конкретной (machine_id, product_code)."""
    limit = limit or config.TRAIN_POINTS
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=config.LOKI_MAX_QUERY_DAYS)
    logql = ('{service_name="%s", event="%s"}'
             ' | json'
             ' | entity_id="%s"'
             % (config.SENSOR_SERVICE, config.SENSOR_EVENT, machine_id))
    records = query_range(logql, start, end, limit=limit, max_lines=limit)
    # фильтруем по product_code на стороне клиента (Loki не индексирует вложенный JSON)
    filtered = [r for r in records
                if (r.get("details") or {}).get("product_code") == product_code]
    logger.info("loki_fetch_machine", extra={"details": {
        "machine_id": machine_id, "product_code": product_code,
        "fetched": len(records), "filtered": len(filtered)}})
    return filtered


def fetch_recent(max_lines: int = None) -> list[dict]:
    """Последние max_lines sensor_reading по ВСЕМ станкам одним лёгким запросом.

    Селектор только по меткам ({service,event}, без `| json`) — Loki быстро отдаёт
    сырые строки, JSON парсим в Python (records_to_long). Для Prophet: один bulk
    вместо тяжёлых per-station `| json | entity_id=` (entity_id не индексируется как
    метка). Разрез по (станок, контекст) — на стороне клиента (features.prophet_frames).
    """
    max_lines = max_lines or config.TRAIN_FETCH_LIMIT
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=config.LOKI_MAX_QUERY_DAYS)
    logql = '{service_name="%s", event="%s"}' % (config.SENSOR_SERVICE, config.SENSOR_EVENT)
    records = query_range(logql, start, end, max_lines=max_lines)
    logger.info("loki_fetch_recent", extra={"details": {
        "records": len(records), "max_lines": max_lines}})
    return records


def fetch_for_training() -> list[dict]:
    """Последние TRAIN_FETCH_LIMIT sensor_reading для обучения.

    Не использует временное окно — запрашивает просто последние N записей
    с direction=backward. Работает при любой скорости симулятора.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=config.LOKI_MAX_QUERY_DAYS)
    logql = '{service_name="%s", event="%s"}' % (config.SENSOR_SERVICE, config.SENSOR_EVENT)
    # limit НЕ задаём: размер страницы остаётся LOKI_PAGE_LIMIT (≤ потолка Loki
    # max_entries_limit_per_query=5000). query_range наберёт TRAIN_FETCH_LIMIT
    # за несколько страниц (direction=backward).
    records = query_range(logql, start, end, max_lines=config.TRAIN_FETCH_LIMIT)
    logger.info("loki_fetch_training", extra={"details": {"records": len(records)}})
    return records


def ping() -> bool:
    try:
        r = requests.get(f"{config.LOKI_URL}/ready", timeout=5)
        return r.status_code == 200
    except Exception:
        return False
