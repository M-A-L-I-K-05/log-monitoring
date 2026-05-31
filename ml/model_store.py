"""Хранение обученных весов на диске (volume) с версионированием.

Каждое обучение создаёт ВЕРСИЮ — отдельный каталог в MODELS_DIR:

    models/
      ACTIVE                      ← текстовый указатель активной версии
      v2026-05-30_12-30-00/
        manifest.json             ← метаданные (когда, на чём, по каким станкам)
        turning-01.joblib         ← сериализованный MachineDetector
        hobbing-01.joblib
        ...

Скоринг идёт по АКТИВНОЙ версии. В WebUI можно обучить новую версию,
переключиться на любую сохранённую («подключить модель») или удалить.

Веса замораживаются: обучил один раз на чистом baseline → лежат на диске,
при перезапуске контейнера загружаются, переобучать каждый прогон не нужно.
"""
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone

import joblib

import config
from detectors import MachineDetector

logger = logging.getLogger("ml.model_store")

ACTIVE_FILE = "ACTIVE"
MANIFEST = "manifest.json"
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _root() -> str:
    os.makedirs(config.MODELS_DIR, exist_ok=True)
    return config.MODELS_DIR


def _version_dir(version: str) -> str:
    return os.path.join(_root(), version)


def _slug(tag: str | None) -> str:
    if not tag:
        return ""
    return _SLUG_RE.sub("-", tag.strip())[:40].strip("-")


def _new_version_name(tag: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("v%Y-%m-%d_%H-%M-%S")
    slug = _slug(tag)
    return f"{stamp}__{slug}" if slug else stamp


# ─── сохранение ────────────────────────────────────────────────
def save_version(detectors: dict[str, MachineDetector],
                 tag: str | None = None,
                 extra: dict | None = None,
                 make_active: bool = True) -> dict:
    """Сохраняет набор обученных детекторов как новую версию. Возвращает манифест."""
    version = _new_version_name(tag)
    vdir = _version_dir(version)
    os.makedirs(vdir, exist_ok=True)

    machines = []
    for mid, det in detectors.items():
        if not det.fitted:
            continue
        joblib.dump(det, os.path.join(vdir, f"{mid}.joblib"))
        machines.append(det.meta())

    manifest = {
        "version": version,
        "tag": tag or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "n_machines": len(machines),
        "machines": machines,
        "contamination": config.CONTAMINATION,
        **(extra or {}),
    }
    with open(os.path.join(vdir, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    if make_active:
        set_active(version)
    logger.info("model_version_saved", extra={"details": {
        "version": version, "n_machines": len(machines), "active": make_active}})
    return manifest


# ─── загрузка ──────────────────────────────────────────────────
def load_version(version: str) -> dict[str, MachineDetector]:
    vdir = _version_dir(version)
    if not os.path.isdir(vdir):
        raise FileNotFoundError(f"версия модели не найдена: {version}")
    detectors: dict[str, MachineDetector] = {}
    for fname in os.listdir(vdir):
        if not fname.endswith(".joblib"):
            continue
        mid = fname[: -len(".joblib")]
        try:
            detectors[mid] = joblib.load(os.path.join(vdir, fname))
        except Exception as exc:  # повреждённый/несовместимый файл — пропускаем
            logger.error("model_load_failed", extra={"details": {
                "version": version, "machine_id": mid, "error": str(exc)}})
    logger.info("model_version_loaded", extra={"details": {
        "version": version, "n_machines": len(detectors)}})
    return detectors


def read_manifest(version: str) -> dict | None:
    path = os.path.join(_version_dir(version), MANIFEST)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def list_versions() -> list[dict]:
    """Все сохранённые версии (новые сверху), с пометкой активной."""
    root = _root()
    active = get_active()
    out = []
    for name in sorted(os.listdir(root), reverse=True):
        vdir = os.path.join(root, name)
        if not os.path.isdir(vdir):
            continue
        man = read_manifest(name) or {"version": name, "machines": [],
                                      "n_machines": 0, "tag": "", "created_at": None}
        man["active"] = (name == active)
        # не тянем тяжёлые поля фич каждого станка в список — оставляем сводку
        man["machine_ids"] = [m.get("machine_id") for m in man.get("machines", [])]
        man.pop("machines", None)
        out.append(man)
    return out


# ─── активная версия ───────────────────────────────────────────
def get_active() -> str | None:
    path = os.path.join(_root(), ACTIVE_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            version = fh.read().strip()
    except Exception:
        return None
    if version and os.path.isdir(_version_dir(version)):
        return version
    return None


def set_active(version: str) -> None:
    if not os.path.isdir(_version_dir(version)):
        raise FileNotFoundError(f"версия модели не найдена: {version}")
    with open(os.path.join(_root(), ACTIVE_FILE), "w", encoding="utf-8") as fh:
        fh.write(version)
    logger.info("model_version_activated", extra={"details": {"version": version}})


# ─── удаление ──────────────────────────────────────────────────
def delete_version(version: str) -> bool:
    vdir = _version_dir(version)
    if not os.path.isdir(vdir):
        return False
    shutil.rmtree(vdir)
    if get_active() == version:
        # активная удалена — указатель снимаем
        try:
            os.remove(os.path.join(_root(), ACTIVE_FILE))
        except FileNotFoundError:
            pass
    logger.info("model_version_deleted", extra={"details": {"version": version}})
    return True


def delete_all_versions() -> int:
    """Удаляет ВСЕ версии весов и указатель активной. Возвращает число удалённых."""
    root = _root()
    n = 0
    for name in os.listdir(root):
        vdir = os.path.join(root, name)
        if os.path.isdir(vdir):
            shutil.rmtree(vdir)
            n += 1
    try:
        os.remove(os.path.join(root, ACTIVE_FILE))
    except FileNotFoundError:
        pass
    logger.info("model_all_versions_deleted", extra={"details": {"count": n}})
    return n
