"""Дорожка видео на таймлайне: полоса маленьких кадров из лёгкой копии.

Кадры берутся через равный шаг времени файла и складываются в одну картинку
(сетка cols × rows), рядом — описание сетки в JSON. Таймлайн сам переводит
время монтажа во время файла и рисует нужный кадр.

Точность — до полсекунды: раскодируются только опорные кадры лёгкой копии
(они идут раз в секунду), поэтому кадр k — ближайший опорный к k × шаг.
Для обзора этого хватает и выходит быстро: час видео — несколько секунд.
Резать по этим кадрам нельзя, точный стык — только пробный ролик.

Картинка лежит рядом с лёгкой копией и считается заново, если копия новее.
"""

import json
import math
import os
import threading
from pathlib import Path

import numpy as np

from longform.ffmpeg import QUIET, CancelCheck, run
from longform.probe import probe_cached

VERSION = 1
HEIGHT = 54  # высота кадра в полосе, пикселей
COLS = 20
MIN_STEP_S = 2
MAX_THUMBS = 600

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def step_seconds(duration_s: float) -> int:
    """Шаг между кадрами: не чаще раза в 2 с и не больше 600 кадров на файл."""
    return max(MIN_STEP_S, math.ceil(duration_s / MAX_THUMBS))


def paths(proxy: Path) -> tuple[Path, Path]:
    stem = f"{proxy.stem}.thumbs-v{VERSION}"
    return proxy.with_name(stem + ".jpg"), proxy.with_name(stem + ".json")


def _fresh(proxy: Path, image: Path, meta: Path) -> bool:
    if not (image.exists() and meta.exists()):
        return False
    return min(image.stat().st_mtime, meta.stat().st_mtime) >= proxy.stat().st_mtime


def build(proxy: Path, cancel: CancelCheck | None = None) -> dict:
    """Собрать картинку-сетку и описание; вернуть описание."""
    image, meta = paths(proxy)
    info = probe_cached(proxy)
    video = info.video
    duration_s = float(video.frame_count / video.fps)
    step = step_seconds(duration_s)
    tile_h = HEIGHT
    tile_w = max(2, round(HEIGHT * video.width / video.height / 2) * 2)
    raw = run(["ffmpeg", *QUIET, "-skip_frame", "nokey", "-i", str(proxy), "-map", "0:v:0", "-an",
               "-vf", f"fps=1/{step}:round=near,scale={tile_w}:{tile_h}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], cancel=cancel)
    size = tile_w * tile_h * 3
    count = len(raw) // size
    if count == 0:
        raise ValueError("в лёгкой копии не нашлось кадров")
    frames = np.frombuffer(raw[: count * size], np.uint8).reshape(count, tile_h, tile_w, 3)
    cols = min(COLS, count)
    rows = -(-count // cols)
    sheet = np.zeros((rows * tile_h, cols * tile_w, 3), np.uint8)
    for i in range(count):
        r, c = divmod(i, cols)
        sheet[r * tile_h:(r + 1) * tile_h, c * tile_w:(c + 1) * tile_w] = frames[i]
    tmp_image = image.with_name(image.name + ".part.jpg")
    tmp_raw = image.with_name(image.name + ".part.rgb")
    tmp_raw.write_bytes(sheet.tobytes())
    try:
        run(["ffmpeg", *QUIET, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{cols * tile_w}x{rows * tile_h}",
             "-i", str(tmp_raw), "-frames:v", "1", "-q:v", "5", str(tmp_image)], cancel=cancel)
    finally:
        tmp_raw.unlink(missing_ok=True)
    data = {"version": VERSION, "step_us": step * 1_000_000, "count": count, "cols": cols, "rows": rows,
            "tile_w": tile_w, "tile_h": tile_h, "accuracy_us": 500_000}
    tmp_meta = meta.with_name(meta.name + ".part")
    tmp_meta.write_text(json.dumps(data))
    os.replace(tmp_image, image)
    os.replace(tmp_meta, meta)
    return data


def ensure(proxy: Path, cancel: CancelCheck | None = None) -> dict:
    """Описание сетки; собрать, если нет или копия новее. Два запроса
    одновременно не собирают одно и то же дважды."""
    image, meta = paths(proxy)
    with _locks_guard:
        lock = _locks.setdefault(proxy, threading.Lock())
    with lock:
        if _fresh(proxy, image, meta):
            return json.loads(meta.read_text())
        return build(proxy, cancel)
