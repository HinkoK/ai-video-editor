"""Хранилище проекта: сколько места занимает и что можно стереть (идея из kadr).

Граница одна: восстановимое и невосстановимое. Восстановимое программа
соберёт заново по запросу, и проект, открытый через год, его подхватит.
Невосстановимое — готовые ролики: их программа сама не стирает никогда.
"""

import shutil
import sqlite3
from pathlib import Path

from longform.edl import EdlError
from longform.ffmpeg import RESERVE_BYTES
from longform.store import Conflict, _one, _only_user
from longform.workspace import Workspace

CATEGORIES = [
    # ключ, папка, название, можно стирать, пояснение
    ("proxies", "proxies", "лёгкие копии для просмотра", True, "пересоберутся по кнопке, минуты на час видео"),
    ("previews", "previews", "пробные ролики стыков", True, "пересоберутся при следующем просмотре"),
    ("cache", "cache", "звук исходников (WAV) и прочий кеш", True, "извлечётся заново при следующей задаче"),
    ("transcripts", "transcripts", "куски расшифровки", False,
     "расшифровка уже в базе; куски нужны, чтобы продолжить прерванную"),
    ("analysis", "analysis", "громкость и паузы", False, "маленькие, без них не выровнять границы"),
    ("exports", "exports", "готовые ролики", False, "невосстановимое: программа их не стирает"),
]


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def usage(conn: sqlite3.Connection, ws: Workspace, pid: str) -> dict:
    _one(conn, "SELECT id FROM projects WHERE id = ?", pid)
    root = ws.root / "projects" / pid
    cats = [{"key": key, "title": title, "bytes": _size(root / folder), "recoverable": recoverable, "note": note}
            for key, folder, title, recoverable, note in CATEGORIES]
    return {"categories": cats, "free_bytes": shutil.disk_usage(ws.root).free, "reserve_bytes": RESERVE_BYTES}


def clean(conn: sqlite3.Connection, ws: Workspace, pid: str, key: str, actor: str) -> dict:
    """Стереть восстановимую категорию. Только автор и только когда ничего не считается."""
    _only_user(actor, "чистить хранилище")
    cat = next((c for c in CATEGORIES if c[0] == key), None)
    if cat is None or not cat[3]:
        raise EdlError("not_recoverable", "эту часть стирать нельзя: её не восстановить или она нужна")
    busy = conn.execute("SELECT COUNT(*) FROM jobs WHERE project_id = ? AND state IN ('queued', 'running')",
                        (pid,)).fetchone()[0]
    if busy:
        raise Conflict("jobs_running", "идут задачи: их файлы могут понадобиться прямо сейчас, очистите позже")
    folder = ws.root / "projects" / pid / cat[1]
    if folder.exists():
        shutil.rmtree(folder)
    return usage(conn, ws, pid)
