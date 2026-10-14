"""Снимки страниц для вставок в ролик (шаблон page_insert).

Автор называет страницу — документацию, каталог, сервис, — и на несколько
секунд её показывают во весь кадр в рамке. Claude снимает страницу по ссылке: Chrome без окна (graphics/capture.mjs),
интернет только для снимка, запись автора никуда не уходит. Страницу, закрытую
входом, автор подкладывает своим снимком (import_file, только автор).

Снимок — картинка в папке проекта pages/<id>.png, id — начало sha256 картинки:
один и тот же снимок не хранится дважды, а рисунок графики с ним кешируется.
"""

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

from longform.db import write
from longform.edl import EdlError
from longform.ffmpeg import QUIET, run
from longform.graphics import GRAPHICS_DIR
from longform.store import NotFound, _now, _only_user
from longform.workspace import Workspace

CAPTURE_TIMEOUT_S = 90
# Ширина окна, в котором снимается страница: в узком окне сайты документации
# прячут боковые меню — остаётся колонка текста
CSS_WIDTH = 900
CSS_WIDTHS = (600, 1600)
MAX_CSS_HEIGHT = 4000  # снимается верх страницы: вставка показывает начало и немного прокрутки
PAGE_ID = re.compile(r"[0-9a-f]{16}")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def path(ws: Workspace, pid: str, page_id: str) -> Path:
    return ws.project(pid, "pages") / f"{page_id}.png"


def _png_size(p: Path) -> tuple[int, int]:
    head = p.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise EdlError("bad_image", "снимок — не PNG")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def _color(value: str) -> str:
    """Цвет фона в #rrggbb; непонятный — белый."""
    m = re.fullmatch(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", (value or "").strip())
    if m and (m.group(4) is None or float(m.group(4)) > 0):
        return "#" + "".join(f"{min(int(x), 255):02x}" for x in m.groups()[:3])
    return value.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", value or "") else "#ffffff"


def _corner(png: Path) -> str:
    """Цвет левого верхнего угла картинки — фон своего снимка автора."""
    raw = subprocess.run(["ffmpeg", *QUIET, "-i", str(png), "-vf", "crop=1:1:0:0", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return "#" + raw[:3].hex() if len(raw) >= 3 else "#ffffff"


def _keep(conn: sqlite3.Connection, ws: Workspace, pid: str, png: Path, *, url: str | None, title: str,
          actor: str, bg: str) -> dict:
    """Положить готовую картинку в папку проекта и записать в базу."""
    page_id = hashlib.sha256(png.read_bytes()).hexdigest()[:16]
    width, height = _png_size(png)
    dst = path(ws, pid, page_id)
    if dst.exists():
        png.unlink()
    else:
        png.rename(dst)
    with write(conn):
        conn.execute(
            "INSERT OR IGNORE INTO pages (id, project_id, url, title, width, height, bg, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (page_id, pid, url, title[:200], width, height, bg, actor, _now()))
    return get_page(conn, pid, page_id)


# Колонка текста во вставке — по центру кадра и на такую долю ширины рамки
#
COLUMN_SHARE = 0.8


def content_box(path: Path) -> dict:
    """Где на снимке содержимое (колонка текста) по ширине и как поставить
    page_insert, чтобы оно встало по центру рамки: {left, right, center,
    cut_right, zoom, cx}. Доли ширины снимка; смотрит верх снимка."""
    import numpy as np

    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    w, h = (int(x) for x in out.split(","))
    hh = min(h, 2400)
    raw = subprocess.run(["ffmpeg", *QUIET, "-i", str(path), "-vf", f"crop={w}:{hh}:0:0", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.uint8)[: w * hh].reshape(hh, w).astype(np.int16)
    bg = np.median(a[:, : max(1, w // 200)])
    ink = (np.abs(a - bg) > 40).mean(axis=0)
    cols = np.where(ink > 0.01)[0]
    if len(cols) == 0:
        return {"left": 0.0, "right": 1.0, "center": 0.5, "cut_right": False, "zoom": 1.0, "cx": 0.5}
    left, right = cols.min() / w, (cols.max() + 1) / w
    center = (left + right) / 2
    zoom = min(2.5, max(0.5, COLUMN_SHARE / (right - left)))
    if abs(center - 0.5) > 0.03:
        zoom = max(zoom, 1.05)  # сдвинуть колонку к центру можно только крупнее рамки (cx)
    cx = min(1.0, max(0.0, (center * zoom - 0.5) / (zoom - 1))) if zoom > 1 else 0.5
    edge = max(1, w // 100)
    return {"left": round(left, 3), "right": round(right, 3), "center": round(center, 3),
            "cut_right": bool(ink[-edge:].mean() > 0.01), "zoom": round(zoom, 2), "cx": round(cx, 3)}


def capture(conn: sqlite3.Connection, ws: Workspace, pid: str, url: str, actor: str,
            css_width: int = CSS_WIDTH, find: str | None = None) -> dict:
    """Снять страницу по ссылке. Возвращает запись снимка; с фразой find —
    ещё marks: где её строки на снимке (доли, [x0, y0, x1, y1]) для маркера
    маркера, или None, если фразы на снятой части страницы нет."""
    if not CSS_WIDTHS[0] <= css_width <= CSS_WIDTHS[1]:
        raise EdlError("bad_width", f"ширина окна от {CSS_WIDTHS[0]} до {CSS_WIDTHS[1]}")
    if actor not in ("user", "agent"):
        raise EdlError("forbidden", "неизвестный автор")
    if not re.match(r"https?://[^\s/]+", url or "") or len(url) > 2000:
        raise EdlError("bad_url", "нужна ссылка http(s)://…")
    if not (GRAPHICS_DIR / "node_modules").exists():
        raise EdlError("no_graphics", "графика не установлена: выполните npm install в папке graphics")
    tmp = ws.project(pid, "pages") / f"capture-{uuid.uuid4().hex}.png"
    try:
        done = subprocess.run(["node", str(GRAPHICS_DIR / "capture.mjs"), url, str(tmp), str(css_width),
                               str(MAX_CSS_HEIGHT), *([find] if find else [])],
                              capture_output=True, text=True, timeout=CAPTURE_TIMEOUT_S)
        if done.returncode != 0 or not tmp.exists():
            reason = (done.stderr.strip().splitlines() or ["неизвестная ошибка"])[-1][:300]
            raise EdlError("capture_failed", f"не удалось снять страницу: {reason}")
        meta = json.loads(done.stdout.strip().splitlines()[-1])
        page = _keep(conn, ws, pid, tmp, url=url, title=meta.get("title", ""), actor=actor,
                     bg=_color(meta.get("bg", "")))
        page["content"] = content_box(ws.project(pid, "pages") / f"{page['id']}.png")
        if find:
            page["marks"] = meta.get("marks")
        return page
    except subprocess.TimeoutExpired:
        raise EdlError("capture_failed", "страница не открылась за 90 секунд") from None
    finally:
        tmp.unlink(missing_ok=True)


def import_file(conn: sqlite3.Connection, ws: Workspace, pid: str, file: str, title: str, actor: str) -> dict:
    """Свой снимок автора (страница за входом, скриншот приложения). Файл не меняется."""
    _only_user(actor, "подкладывать свои снимки")
    src = Path(file).expanduser()
    if src.suffix.lower() not in IMAGE_SUFFIXES or not src.is_file():
        raise EdlError("bad_image", "нужна картинка PNG, JPG или WEBP")
    tmp = ws.project(pid, "pages") / f"import-{uuid.uuid4().hex}.png"
    try:
        if src.suffix.lower() == ".png":
            shutil.copyfile(src, tmp)
        else:
            run(["ffmpeg", *QUIET, "-n", "-i", str(src), "-frames:v", "1", str(tmp)])
        return _keep(conn, ws, pid, tmp, url=None, title=title or src.stem, actor=actor, bg=_corner(tmp))
    finally:
        tmp.unlink(missing_ok=True)


def get_page(conn: sqlite3.Connection, pid: str, page_id: str) -> dict:
    row = conn.execute("SELECT * FROM pages WHERE project_id = ? AND id = ?", (pid, page_id)).fetchone()
    if row is None:
        raise NotFound(f"снимка страницы {page_id} нет — сначала capture_page")
    return dict(row)


def list_pages(conn: sqlite3.Connection, pid: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM pages WHERE project_id = ? ORDER BY created_at", (pid,))]


def preview(ws: Workspace, pid: str, page_id: str, width: int = 800, height: int = 1200) -> bytes:
    """Верх снимка уменьшенным — посмотреть, что снято."""
    src = path(ws, pid, page_id)
    return subprocess.run(
        ["ffmpeg", *QUIET, "-i", str(src), "-vf", f"scale={width}:-2,crop=iw:'min(ih,{height})':0:0",
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, check=True).stdout
