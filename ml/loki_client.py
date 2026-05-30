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
    """Тянет записи конкретного события конкретного сервиса за недавнее окно."""
    real_lookback_min = real_lookback_min if real_lookback_min is not None \
        else config.REAL_LOOKBACK_MIN
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=real_lookback_min)
    logql = '{service_name="%s", event="%s"}' % (service, event)
    records = query_range(logql, start, end)
    logger.info("loki_fetch", extra={"details": {
        "service": service, "event": event,
        "real_lookback_min": real_lookback_min, "records": len(records)}})
    return records


def ping() -> bool:
    try:
        r = requests.get(f"{config.LOKI_URL}/ready", timeout=5)
        return r.status_code == 200
    except Exception:
        return False
