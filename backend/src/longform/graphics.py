"""Моушн-графика: шаблоны поверх кадра (graphics/ в корне репозитория).

Графика — участок времени записи экрана, шаблон и его поля. При сборке
каждый видимый кусок рисуется в кадры с прозрачностью (Chrome без окна,
graphics/render.mjs) и накладывается на ролик. Кадры рисунков кешируются.

Автоматически: у каждого сокращённого ожидания (правка waiting) на стыке
ставится пометка «ожидание сокращено» — честно о времени (решение автора).
"""

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from longform.db import write
from longform.edl import CompiledEdl, EdlError
from longform.ffmpeg import CancelCheck, run
from longform.store import Conflict, Forbidden, _id, _now, _one, _only_user
from longform.timebase import frame_at, frame_to_us, us_to_frame

GRAPHICS_DIR = Path(__file__).resolve().parents[3] / "graphics"
VERSION = 1  # меняется вместе с шаблонами и рисованием кадров: старые рисунки в кеше не годятся
RENDER_FPS = 30  # графика рисуется с этой частотой и дублируется до частоты ролика
MIN_US = 1_500_000  # короче не успеть прочитать: появление, чтение, уход
MAX_US = 60_000_000
WAITING_BADGE_US = 1_600_000
MIN_PIECE_US = 800_000  # остаток графики после выреза короче — не показываем
# Свой минимум у шаблона (graphics/templates.mjs): вставке страницы хватает секунды
MIN_US_BY_TEMPLATE = {"page_insert": 1_000_000}
TEMPLATES = ("lower_third", "term", "keyword", "chapter", "steps", "compare", "checklist", "command", "page_insert")
# Начало и конец графики во весь кадр (graphics/templates.mjs): enter — cut (сплошная с первого
# кадра, без затемнения), pan (переход PAN: кадр под графикой уезжает влево, графика въезжает
# справа — render/select.py); hold: true — фон до последнего кадра: следующая встаёт встык
ENTERS = ("cut", "pan")
PAN_US = 300_000  # длина перехода PAN
# Нужен снимок страницы: путь и размер подставляет исполнитель
PAGE_TEMPLATES = ("page_insert",)


def min_us(template: str) -> int:
    return MIN_US_BY_TEMPLATE.get(template, MIN_US)


def _share(p: dict, key: str, lo: float, hi: float, default: float) -> float:
    v = p.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise EdlError("bad_props", f"{key}: число от {lo} до {hi}")
    return round(float(v), 4)


def _choice(p: dict, key: str, options: tuple[str, ...]) -> str | None:
    v = p.get(key)
    if v in (None, ""):
        return None
    if v not in options:
        raise EdlError("bad_props", f"{key}: {' | '.join(options)}")
    return v


def _text(v, lo: int, hi: int, name: str) -> str:
    if not isinstance(v, str) or not lo <= len(v.strip()) <= hi:
        raise EdlError("bad_props", f"{name}: текст от {lo} до {hi} знаков")
    return v.strip()


def _opt(p: dict, key: str, hi: int) -> str | None:
    return _text(p[key], 1, hi, key) if p.get(key) else None


def page_refs(template: str, props: dict) -> list[str]:
    """Снимки страниц, на которые ссылается графика."""
    return [props["page"]] if template in PAGE_TEMPLATES else []


def with_pages(template: str, props: dict, page) -> dict:
    """Подставить снимок: page(id) → {src, w, h, bg}."""
    return {**props, **page(props["page"])} if template in PAGE_TEMPLATES else dict(props)


def _missing_page(conn: sqlite3.Connection, pid: str, template: str, props: dict) -> None:
    for ref in page_refs(template, props):
        if not conn.execute("SELECT 1 FROM pages WHERE project_id = ? AND id = ?", (pid, ref)).fetchone():
            raise EdlError("no_page", f"снимка страницы {ref} нет — сначала capture_page")


def _page_id(p: dict) -> str:
    page = p.get("page")
    if not isinstance(page, str) or not re.fullmatch(r"[0-9a-f]{16}", page):
        raise EdlError("bad_props", "page: id снимка страницы (capture_page)")
    return page


def _list(v, lo: int, hi: int, name: str) -> list:
    if not isinstance(v, list) or not lo <= len(v) <= hi:
        raise EdlError("bad_props", f"{name}: от {lo} до {hi} пунктов")
    return v


def _place(v) -> dict:
    """Где графика стоит, если автор сдвинул её на мониторе: сдвиг в долях кадра
    и масштаб (как Motion в Premiere); рисует обёртка в graphics/templates.mjs."""
    if not isinstance(v, dict):
        raise EdlError("bad_props", "place: {x, y, scale}")
    try:
        out = {"x": _share(v, "x", -1, 1, 0), "y": _share(v, "y", -1, 1, 0), "scale": _share(v, "scale", 0.3, 3, 1)}
    except EdlError as e:
        raise EdlError("bad_props", f"place: {e.message}") from None
    if set(v) - set(out):
        raise EdlError("bad_props", "place: только x, y и scale")
    return out


def validate(template: str, props: dict) -> dict:
    """Поля шаблона: обязательные есть, длины такие, что текст читается."""
    p = dict(props or {})
    place = p.pop("place", None)
    # У вставки страницы своё enter (cut, up) — его проверит она сама; общее pan ей тоже можно
    enter = p.get("enter")
    if template != "page_insert" or enter == "pan":
        p.pop("enter", None)
    else:
        enter = None
    hold = p.pop("hold", None)
    out = _validate(template, p)
    if place is not None:
        out["place"] = _place(place)
    if enter is not None:
        if enter not in ENTERS:
            raise EdlError("bad_props", "enter: cut — сразу сплошная, без затемнения; pan — переход PAN")
        out["enter"] = enter
    if hold is not None:
        if not isinstance(hold, bool):
            raise EdlError("bad_props", "hold: true — фон держится до конца, следующая графика встаёт встык")
        if hold:
            out["hold"] = True
    return out


def _validate(template: str, p: dict) -> dict:
    if template == "lower_third":
        out = {"title": _text(p.get("title"), 1, 80, "title"), "subtitle": _opt(p, "subtitle", 120)}
    elif template == "term":
        out = {"term": _text(p.get("term"), 1, 60, "term"), "definition": _text(p.get("definition"), 1, 220, "definition")}
    elif template == "keyword":
        out = {"text": _text(p.get("text"), 1, 90, "text")}
    elif template == "chapter":
        out = {"title": _text(p.get("title"), 1, 80, "title"),
               "number": str(p["number"])[:3] if p.get("number") not in (None, "") else None}
    elif template == "time_compressed":
        out = {"text": _opt(p, "text", 40)}
    elif template == "steps":
        steps = [_text(s, 1, 60, "steps[]") for s in _list(p.get("steps"), 2, 7, "steps")]
        active = p.get("active", -1)
        if not isinstance(active, int) or not -1 <= active < len(steps):
            raise EdlError("bad_props", "active: номер текущего этапа с 0 или -1")
        out = {"title": _opt(p, "title", 80), "steps": steps, "active": active}
    elif template == "compare":
        def side(v, name):
            if not isinstance(v, dict):
                raise EdlError("bad_props", f"{name}: {{title, items}}")
            return {"title": _text(v.get("title"), 1, 40, f"{name}.title"),
                    "items": [_text(x, 1, 80, f"{name}.items[]") for x in _list(v.get("items"), 1, 6, f"{name}.items")]}
        out = {"title": _opt(p, "title", 80), "left": side(p.get("left"), "left"), "right": side(p.get("right"), "right"),
               "note": _opt(p, "note", 160)}
    elif template == "checklist":
        items = []
        for it in _list(p.get("items"), 1, 8, "items"):
            if not isinstance(it, dict) or it.get("state") not in ("done", "now", "todo"):
                raise EdlError("bad_props", "items[]: {text, state: done | now | todo}")
            items.append({"text": _text(it.get("text"), 1, 80, "items[].text"), "state": it["state"]})
        out = {"title": _opt(p, "title", 80), "items": items}
    elif template == "command":
        # Команда — ровно как в записи: без обрезки пробелов внутри
        text = p.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= 200:
            raise EdlError("bad_props", "text: команда от 1 до 200 знаков")
        out = {"text": text, "caption": _opt(p, "caption", 100)}
    elif template == "page_insert":
        # top — с какой доли высоты снимка начать, scroll — на сколько высот кадра
        # прокрутить за вставку, zoom и cx — крупнее и куда по ширине смотреть
        out = {"page": _page_id(p), "top": _share(p, "top", 0, 0.95, 0), "scroll": _share(p, "scroll", 0, 1, 0.15),
               "zoom": _share(p, "zoom", 0.5, 2.5, 1), "cx": _share(p, "cx", 0, 1, 0.5),
               "enter": _choice(p, "enter", ("cut", "up"))}
    else:
        raise EdlError("bad_template", "шаблон: " + ", ".join(t for t in TEMPLATES) + " (time_compressed ставится сам)")
    return {k: v for k, v in out.items() if v is not None}


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["props"] = json.loads(d.pop("props_json"))
    return d


def add_graphic(conn: sqlite3.Connection, pid: str, *, actor: str, asset_id: str, src_start_us: int, src_end_us: int,
                template: str, props: dict, goal: str) -> dict:
    if actor not in ("user", "agent"):
        raise Forbidden("неизвестный автор")
    if not goal.strip():
        raise EdlError("no_goal", "у графики должна быть цель: что зритель поймёт")
    if template == "time_compressed":
        raise EdlError("bad_template", "пометку «ожидание сокращено» ставит сам редактор у правки waiting")
    props = validate(template, props)
    _missing_page(conn, pid, template, props)
    asset = _one(conn, "SELECT * FROM assets WHERE id = ? AND project_id = ?", asset_id, pid)
    if asset["role"] != "main":
        raise EdlError("not_main", "графика ставится по записи экрана")
    duration = frame_to_us(asset["frame_count"], Fraction(asset["fps_num"], asset["fps_den"]))
    if not 0 <= src_start_us < src_end_us <= duration:
        raise EdlError("out_of_bounds", "графика за пределами записи")
    if not min_us(template) <= src_end_us - src_start_us <= MAX_US:
        least = f"{min_us(template) / 1e6:.1f}".replace(".", ",")
        raise EdlError("bad_duration", f"графика «{template}» от {least} до 60 секунд: "
                                       "короче не прочитать, дольше — уже не акцент")
    gid = _id("gfx")
    with write(conn):
        clash = conn.execute(
            "SELECT id FROM graphics WHERE project_id = ? AND asset_id = ? AND review != 'reverted' "
            "AND src_start_us < ? AND src_end_us > ?",
            (pid, asset_id, src_end_us, src_start_us)).fetchone()
        if clash:
            raise Conflict("graphics_overlap", f"графика пересекается с другой ({clash[0]}): две сразу не читаются")
        conn.execute(
            "INSERT INTO graphics (id, project_id, asset_id, src_start_us, src_end_us, template, props_json, goal, "
            "created_by, review, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, pid, asset_id, src_start_us, src_end_us, template, json.dumps(props, ensure_ascii=False),
             goal.strip()[:500], actor, "kept" if actor == "user" else "pending", _now(),
             _now() if actor == "user" else None))
    return _row(_one(conn, "SELECT * FROM graphics WHERE id = ?", gid))


def update_props(conn: sqlite3.Connection, pid: str, gid: str, props: dict, actor: str) -> dict:
    """Автор поправил графику на мониторе: положение, масштаб, текст. Графика
    считается проверенной им; рисунок пересоберётся (поля входят в отпечаток)."""
    _only_user(actor, "править графику")
    with write(conn):
        row = _one(conn, "SELECT * FROM graphics WHERE id = ? AND project_id = ?", gid, pid)
        clean = validate(row["template"], props)
        _missing_page(conn, pid, row["template"], clean)
        conn.execute("UPDATE graphics SET props_json = ?, review = 'kept', decided_at = ? WHERE id = ?",
                     (json.dumps(clean, ensure_ascii=False), _now(), gid))
    return _row(_one(conn, "SELECT * FROM graphics WHERE id = ?", gid))


def review_graphic(conn: sqlite3.Connection, pid: str, gid: str, action: str, actor: str) -> dict:
    _only_user(actor, "проверять графику")
    if action not in ("keep", "revert"):
        raise EdlError("bad_action", "keep или revert")
    with write(conn):
        _one(conn, "SELECT id FROM graphics WHERE id = ? AND project_id = ?", gid, pid)
        conn.execute("UPDATE graphics SET review = ?, decided_at = ? WHERE id = ?",
                     ("kept" if action == "keep" else "reverted", _now(), gid))
    return _row(_one(conn, "SELECT * FROM graphics WHERE id = ?", gid))


def _edited_ranges(edl: CompiledEdl, asset_id: str, start_us: int, end_us: int) -> list[tuple[int, int]]:
    if asset_id not in edl.sources:
        return []
    fps = edl.sources[asset_id].fps
    a, b = us_to_frame(start_us, fps), us_to_frame(end_us, fps)
    out = []
    for c in edl.clips:
        if c.asset_id != asset_id:
            continue
        lo, hi = max(a, c.src_start_frame), min(b, c.src_end_frame)
        if lo < hi:
            out.append((c.out_start_frame + lo - c.src_start_frame, c.out_start_frame + hi - c.src_start_frame))
    return out


def split_by(conn: sqlite3.Connection, pid: str, asset_id: str, points_us: list[int | None]) -> list[dict]:
    """Неотменённая графика, которую режет граница перестановки (step-29):
    точка строго внутри графики. Части такой графики в ролике идут не встык
    и рисуются отдельно (overlays, _joined) — правке ставится пометка
    splits_graphic, а Claude получает предупреждение."""
    points = [x for x in points_us if x is not None]
    out = []
    for r in conn.execute("SELECT * FROM graphics WHERE project_id = ? AND asset_id = ? AND review != 'reverted' "
                          "ORDER BY src_start_us", (pid, asset_id)):
        if any(r["src_start_us"] < x < r["src_end_us"] for x in points):
            out.append(_row(r))
    return out


def list_graphics(conn: sqlite3.Connection, pid: str, edl: CompiledEdl | None = None) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM graphics WHERE project_id = ? ORDER BY created_at", (pid,)):
        d = _row(r)
        d["edited_ranges"] = _edited_ranges(edl, d["asset_id"], d["src_start_us"], d["src_end_us"]) if edl else []
        d["in_revision"] = bool(d["edited_ranges"])
        out.append(d)
    return out


@dataclass(frozen=True)
class Overlay:
    """Рисунок поверх ролика: шаблон и поля, с какого кадра и сколько кадров."""

    template: str
    props: dict
    start: int
    frames: int
    # Переход PAN (кадров): графика въезжает справа, всё под ней уезжает влево
    pan: int = 0
    # На столько кадров рисунок держит последний кадр: следующая графика въезжает
    # переходом PAN и уводит его с собой влево, лицо под ним не показывается
    extend: int = 0

    def key(self, width: int, height: int) -> str:
        data = [VERSION, self.template, self.props, self.frames, width, height, RENDER_FPS]
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def _waiting_badges(conn: sqlite3.Connection, pid: str, edl: CompiledEdl) -> list[Overlay]:
    """Стыки сокращённых ожиданий этой версии — с пометкой «ожидание сокращено»."""
    rows = conn.execute(
        "SELECT o.asset_id, o.src_start_us, o.src_end_us FROM proposal_ops o JOIN proposals p ON p.id = o.proposal_id "
        "WHERE p.project_id = ? AND o.reason = 'waiting' AND o.kind = 'remove' AND o.decision = 'accepted' "
        "AND o.review != 'reverted'", (pid,)).fetchall()
    out = []
    fps_out = edl.profile.fps
    badge = max(1, us_to_frame(WAITING_BADGE_US, fps_out))
    seen = set()
    for r in rows:
        if r["asset_id"] not in edl.sources:
            continue
        fps = edl.sources[r["asset_id"]].fps
        if edl.source_to_edited(r["asset_id"], frame_at(r["src_start_us"], fps)) is not None:
            continue  # этот вырез в версии не действует
        joint = edl.source_to_edited(r["asset_id"], us_to_frame(r["src_end_us"], fps))
        if joint is None or joint in seen:
            continue
        seen.add(joint)
        out.append(Overlay("time_compressed", {}, joint, min(badge, edl.total_frames - joint)))
    return [o for o in out if o.frames > 0]


def _joined(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Куски графики, которые в ролике идут встык (склейка внутри графики), —
    один рисунок: иначе на каждой склейке графика появлялась бы заново."""
    out: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if out and out[-1][1] == a:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def overlays(conn: sqlite3.Connection, pid: str, edl: CompiledEdl) -> list[Overlay]:
    """Рисунки для ролика по монтажному листу, по порядку."""
    fps_out = edl.profile.fps
    out: list[Overlay] = []
    for g in conn.execute("SELECT * FROM graphics WHERE project_id = ? AND review != 'reverted'", (pid,)):
        min_piece = us_to_frame(min(MIN_PIECE_US, min_us(g["template"])), fps_out)
        for a, b in _joined(_edited_ranges(edl, g["asset_id"], g["src_start_us"], g["src_end_us"])):
            if b - a >= min_piece:
                props = json.loads(g["props_json"])
                pan = min(b - a, us_to_frame(PAN_US, fps_out)) if props.get("enter") == "pan" else 0
                out.append(Overlay(g["template"], props, a, b - a, pan=pan))
    if _one(conn, "SELECT mark_waiting FROM projects WHERE id = ?", pid)["mark_waiting"]:
        out += _waiting_badges(conn, pid, edl)
    # Графика, за которой встык въезжает другая (PAN), держится, пока та въезжает
    ends = {o.start + o.frames: i for i, o in enumerate(out)}
    for o in out:
        if o.pan and o.start in ends:
            i = ends[o.start]
            out[i] = replace(out[i], extend=o.pan)
    return sorted(out, key=lambda o: o.start)


def fingerprint(conn: sqlite3.Connection, pid: str) -> str:
    rows = [(r["asset_id"], r["src_start_us"], r["src_end_us"], r["template"], r["props_json"])
            for r in conn.execute("SELECT * FROM graphics WHERE project_id = ? AND review != 'reverted' ORDER BY id",
                                  (pid,))]
    mark = _one(conn, "SELECT mark_waiting FROM projects WHERE id = ?", pid)["mark_waiting"]
    return hashlib.sha256(json.dumps([VERSION, rows, mark]).encode()).hexdigest()[:16]


def _png_color_type(path: Path) -> int:
    with path.open("rb") as f:
        head = f.read(26)
    return head[25]  # IHDR: 2 — RGB, 6 — RGBA


def _same_format(frames_dir: Path) -> None:
    """Все кадры рисунка — одного формата. Chrome сохраняет полностью
    закрашенный кадр без прозрачности (RGB), а с прозрачными местами — с ней
    (RGBA). У графики во весь кадр начало и уход прозрачные, середина сплошная:
    на смене формата ffmpeg пересобирает фильтры, счёт кадров начинается
    заново, и графика ложится со сдвигом и лишними кадрами (пробный ролик
    с титром главы выходил на 318 кадров вместо 180). Сплошные кадры
    переписываем с прозрачностью; одинаковые файлы — один раз."""
    frames = sorted(frames_dir.glob("[0-9]*.png"))
    rgb = [p for p in frames if _png_color_type(p) != 6]
    if not rgb or len(rgb) == len(frames):
        return
    done: dict[str, Path] = {}
    for p in rgb:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest in done:
            shutil.copyfile(done[digest], p)
            continue
        tmp = p.with_name(p.stem + ".rgba.png")
        run(["ffmpeg", "-v", "error", "-y", "-i", str(p), "-pix_fmt", "rgba", str(tmp)])
        tmp.replace(p)
        done[digest] = p


def render_frames(overlay: Overlay, fps_out: Fraction, width: int, height: int, cache: Path,
                  cancel: CancelCheck | None = None) -> Path:
    """Кадры рисунка (PNG с прозрачностью, RENDER_FPS) — из кеша или заново."""
    out = cache / overlay.key(width, height)
    if (out / "done").exists():
        return out
    if not (GRAPHICS_DIR / "node_modules").exists():
        raise EdlError("no_graphics", "графика не установлена: выполните npm install в папке graphics")
    count = int(overlay.frames * RENDER_FPS / fps_out) + 2
    part = out.with_name(out.name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    spec = part / "spec.json"
    spec.write_text(json.dumps({"template": overlay.template, "props": overlay.props, "width": width,
                                "height": height, "fps": RENDER_FPS, "frames": count,
                                "duration_ms": float(overlay.frames / fps_out * 1000)}, ensure_ascii=False))
    run(["node", str(GRAPHICS_DIR / "render.mjs"), str(spec), str(part)], cancel=cancel)
    _same_format(part)
    (part / "done").write_text("")
    shutil.rmtree(out, ignore_errors=True)
    part.rename(out)
    return out
