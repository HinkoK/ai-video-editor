"""Слой кадра: участки раскладки и план для сборки (render/compose.py).

Участок — диапазон времени записи экрана и раскладка: screen (экран, по
желанию с приближением области), camera (лицо, по желанию крупнее —
punch, с плавным возвратом — punch.release, с медленным наездом по кускам
между склейками — push, со сменой плана на склейках — alternate), pip
(экран и лицо в углу), side (экран в рамке на тёмном фоне и лицо квадратом
сбоку; по желанию только область экрана, region; опускание экрана —
drop). Переход на
участок — enter: whip (вертикальная шторка). Вне участков действует
раскладка проекта. Участок
переживает правки: вырезанная часть просто не попадает в ролик.

Лицо можно показать только там, где камера снимала: участок с лицом вне
записи камеры собирается экраном (и это видно в плане).
"""

import hashlib
import json
import re
import sqlite3
import subprocess
from fractions import Fraction
from pathlib import Path

from longform.db import write
from longform.edl import CompiledEdl, EdlError
from longform.render.compose import SLOTS, CameraSource, Shot, VisualPlan
from longform.store import Conflict, Forbidden, _id, _now, _one, _only_user
from longform.timebase import frame_to_us, us_to_frame

LAYOUTS = ("screen", "camera", "pip", "side", "broll")
CLIP_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.(mp4|mov)$")
ENTERS = ("whip",)
DEFAULT_PIP = {"slot": "br", "size": 0.22}
SYNCED = ("ok", "weak", "manual")


def _num(value, lo: float, hi: float, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise EdlError("bad_params", f"{name}: нужно число") from None
    if not lo <= v <= hi:
        raise EdlError("bad_params", f"{name}: от {lo} до {hi}")
    return round(v, 4)


def validate_params(layout: str, params: dict | None) -> dict:
    """Проверить и привести параметры раскладки. Доли — от размера кадра."""
    if layout not in LAYOUTS:
        raise EdlError("bad_layout", "раскладка: screen, camera, pip, side или broll")
    params = params or {}
    out: dict = {}
    if layout == "side" and params.get("region"):
        z = params["region"]
        x, y = _num(z.get("x"), 0, 0.9, "region.x"), _num(z.get("y"), 0, 0.9, "region.y")
        w, h = _num(z.get("w"), 0.2, 1, "region.w"), _num(z.get("h"), 0.2, 1, "region.h")
        if x + w > 1.0001 or y + h > 1.0001:
            raise EdlError("bad_params", "область экрана выходит за кадр")
        out["region"] = {"x": x, "y": y, "w": w, "h": h}
    if layout == "side" and params.get("bg") is not None:
        # Видео-фон за рамкой экрана — файл из папки backgrounds (раскладка проекта)
        if not isinstance(params["bg"], str) or not CLIP_NAME.match(params["bg"]):
            raise EdlError("bad_params", "bg: имя видео-фона из папки backgrounds, например background.mp4")
        out["bg"] = params["bg"]
    elif params.get("bg") is not None:
        raise EdlError("bad_params", "bg — только для экрана в рамке (side)")
    if layout == "side" and params.get("face_cx") is not None:
        out["face_cx"] = _num(params["face_cx"], 0, 1, "face_cx")
    if layout == "broll":
        # Свой кадр во весь экран, голос идёт поверх: клип из папки broll и с какой секунды
        clip = params.get("clip")
        if not isinstance(clip, str) or not CLIP_NAME.match(clip):
            raise EdlError("bad_params", "clip: имя клипа из папки broll (list_broll), например C1551.mp4")
        out["clip"] = clip
        out["at"] = _num(params.get("at", 0), 0, 36000, "at")
    elif params.get("clip") is not None or params.get("at") is not None:
        raise EdlError("bad_params", "clip и at — только для своих кадров (broll)")
    if params.get("enter") is not None:
        if params["enter"] not in ENTERS:
            raise EdlError("bad_params", "переход: whip")
        out["enter"] = params["enter"]
    if layout == "screen" and params.get("zoom"):
        z = params["zoom"]
        x, y = _num(z.get("x"), 0, 0.9, "zoom.x"), _num(z.get("y"), 0, 0.9, "zoom.y")
        # Не крупнее ×5: мельче — уже не прочитать честно, лучше переснять
        w, h = _num(z.get("w"), 0.2, 1, "zoom.w"), _num(z.get("h"), 0.2, 1, "zoom.h")
        if x + w > 1.0001 or y + h > 1.0001:
            raise EdlError("bad_params", "область приближения выходит за кадр")
        out["zoom"] = {"x": x, "y": y, "w": w, "h": h}
    if layout in ("camera", "pip") and params.get("punch"):
        pch = params["punch"]
        out["punch"] = {"scale": _num(pch.get("scale"), 1.0, 2.0, "punch.scale"),
                        "cx": _num(pch.get("cx", 0.5), 0, 1, "punch.cx"),
                        "cy": _num(pch.get("cy", 0.5), 0, 1, "punch.cy")}
        if pch.get("release") is not None:
            if layout != "camera":
                raise EdlError("bad_params", "punch.release: только для лица целиком (camera)")
            # Укрупнение рывком на склейке и плавный возврат к общему плану
            out["punch"]["release"] = _num(pch["release"], 0.2, 10, "punch.release")
        if pch.get("attack") is not None:
            if layout != "camera":
                raise EdlError("bad_params", "punch.attack: только для лица целиком (camera)")
            # Зум: лицо плавно приближается за столько секунд, а не рывком
            out["punch"]["attack"] = _num(pch["attack"], 0.1, 5, "punch.attack")
        unknown_p = set(pch) - {"scale", "cx", "cy", "release", "attack"}
        if unknown_p:
            raise EdlError("bad_params", f"punch: неизвестные поля: {', '.join(sorted(unknown_p))}")
    if params.get("drop") is not None:
        if layout != "side":
            raise EdlError("bad_params", "drop: только для экрана в рамке (side)")
        # Экран опускается, сверху освобождается место
        out["drop"] = _num(params["drop"], 0.02, 0.2, "drop")
    if params.get("push") is not None:
        if layout != "camera":
            raise EdlError("bad_params", "push: только для лица целиком (camera)")
        # Медленный наезд на каждом куске между склейками; 0.04 — на 4 % к концу куска
        out["push"] = _num(params["push"], 0.005, 0.15, "push")
    if params.get("alternate") is not None:
        if layout != "camera":
            raise EdlError("bad_params", "alternate: только для лица целиком (camera)")
        if params.get("punch") or params.get("push") is not None:
            raise EdlError("bad_params", "alternate: без punch и push — план меняется только рывком на склейках; "
                                         "главная фраза крупнее — отдельным участком с punch")
        # Смена плана на склейках: куски по очереди общий и крупнее на step
        a = params["alternate"]
        if not isinstance(a, dict):
            raise EdlError("bad_params", "alternate: {step, cx, cy}")
        out["alternate"] = {"step": _num(a.get("step"), 0.03, 0.2, "alternate.step"),
                            "cx": _num(a.get("cx", 0.5), 0, 1, "alternate.cx"),
                            "cy": _num(a.get("cy", 0.5), 0, 1, "alternate.cy")}
        unknown_a = set(a) - {"step", "cx", "cy"}
        if unknown_a:
            raise EdlError("bad_params", f"alternate: неизвестные поля: {', '.join(sorted(unknown_a))}")
    if params.get("face") is not None:
        # Окно лица, выставленное мышью на мониторе: левый верхний угол и ширина, доли кадра
        if layout not in ("side", "pip"):
            raise EdlError("bad_params", "face: только для экрана в рамке (side) и лица в углу (pip)")
        f = params["face"]
        if not isinstance(f, dict):
            raise EdlError("bad_params", "face: {x, y, size}")
        out["face"] = {"x": _num(f.get("x"), -0.2, 1, "face.x"), "y": _num(f.get("y"), -0.2, 1, "face.y"),
                       "size": _num(f.get("size"), 0.05, 0.9, "face.size")}
    if params.get("card") is not None:
        # Рамка экрана, выставленная мышью: левый верхний угол и ширина, доли кадра
        if layout != "side":
            raise EdlError("bad_params", "card: только для экрана в рамке (side)")
        c = params["card"]
        if not isinstance(c, dict):
            raise EdlError("bad_params", "card: {x, y, w}")
        out["card"] = {"x": _num(c.get("x"), -0.5, 1, "card.x"), "y": _num(c.get("y"), -0.5, 1, "card.y"),
                       "w": _num(c.get("w"), 0.1, 1.5, "card.w")}
    if layout == "pip":
        slot = params.get("slot", DEFAULT_PIP["slot"])
        if slot not in SLOTS:
            raise EdlError("bad_params", "угол: br, bl, tr или tl")
        out["slot"] = slot
        out["size"] = _num(params.get("size", DEFAULT_PIP["size"]), 0.12, 0.45, "size")
    unknown = set(params) - {"zoom", "punch", "push", "alternate", "drop", "slot", "size", "region", "face_cx", "enter",
                             "clip", "at", "bg", "face", "card"}
    if unknown:
        raise EdlError("bad_params", f"неизвестные параметры: {', '.join(sorted(unknown))}")
    return out


def project_layout(conn: sqlite3.Connection, pid: str) -> dict:
    """Раскладка вне участков: из проекта, иначе лицо в углу, если есть
    сведённая камера, иначе экран."""
    row = _one(conn, "SELECT layout_json FROM projects WHERE id = ?", pid)
    if row["layout_json"]:
        return json.loads(row["layout_json"])
    has_camera = conn.execute("SELECT 1 FROM assets WHERE project_id = ? AND role = 'camera' AND sync_state IN "
                              f"({','.join('?' * len(SYNCED))})", (pid, *SYNCED)).fetchone()
    return {"layout": "pip", **DEFAULT_PIP} if has_camera else {"layout": "screen"}


def set_project_layout(conn: sqlite3.Connection, pid: str, layout: str, params: dict | None, actor: str) -> dict:
    _only_user(actor, "менять раскладку проекта")
    data = {"layout": layout, **validate_params(layout, params)}
    with write(conn):
        _one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        conn.execute("UPDATE projects SET layout_json = ? WHERE id = ?", (json.dumps(data), pid))
    return data


def cameras(conn: sqlite3.Connection, pid: str) -> dict[str, CameraSource]:
    """Сведённые камеры: id записи экрана -> камера."""
    out = {}
    for r in conn.execute("SELECT * FROM assets WHERE project_id = ? AND role = 'camera' AND sync_to IS NOT NULL "
                          f"AND sync_state IN ({','.join('?' * len(SYNCED))}) ORDER BY position", (pid, *SYNCED)):
        out.setdefault(r["sync_to"], CameraSource(r["id"], r["sync_offset_us"], r["sync_drift_ppm"] or 0.0))
    return out


def camera_covers(conn: sqlite3.Connection, cam: CameraSource, start_us: int, end_us: int) -> bool:
    """Снимала ли камера весь диапазон времени записи экрана."""
    row = _one(conn, "SELECT frame_count, fps_num, fps_den FROM assets WHERE id = ?", cam.asset_id)
    duration = frame_to_us(row["frame_count"], Fraction(row["fps_num"], row["fps_den"]))
    scale = 1 + cam.drift_ppm / 1e6
    return cam.offset_us + start_us * scale >= 0 and cam.offset_us + end_us * scale <= duration


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json"))
    return d


def clip_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def list_broll(clips_dir: Path | None) -> list[dict]:
    """Клипы для вставок: имя, длительность и описание из broll/clips.json (если есть)."""
    if clips_dir is None or not clips_dir.is_dir():
        return []
    notes = {}
    if (clips_dir / "clips.json").exists():
        notes = json.loads((clips_dir / "clips.json").read_text())
    return [{"clip": p.name, "seconds": round(clip_duration(p), 2), **notes.get(p.name, {})}
            for p in sorted(clips_dir.iterdir()) if CLIP_NAME.match(p.name)]


def add_segment(conn: sqlite3.Connection, pid: str, *, actor: str, asset_id: str, src_start_us: int, src_end_us: int,
                layout: str, params: dict | None, goal: str, clips_dir: Path | None = None) -> dict:
    if actor not in ("user", "agent"):
        raise Forbidden("неизвестный автор")
    if not goal.strip():
        raise EdlError("no_goal", "у участка должна быть цель: что зритель должен увидеть или понять")
    params = validate_params(layout, params)
    asset = _one(conn, "SELECT * FROM assets WHERE id = ? AND project_id = ?", asset_id, pid)
    if asset["role"] != "main":
        raise EdlError("not_main", "участок раскладки ставится по записи экрана, а не по камере")
    duration = frame_to_us(asset["frame_count"], Fraction(asset["fps_num"], asset["fps_den"]))
    if not 0 <= src_start_us < src_end_us <= duration:
        raise EdlError("out_of_bounds", "участок за пределами записи")
    if layout in ("camera", "pip", "side"):
        cam = cameras(conn, pid).get(asset_id)
        if cam is None:
            raise EdlError("no_camera", "у этой записи нет сведённой камеры — лицо показать нечем")
        if not camera_covers(conn, cam, src_start_us, src_end_us):
            raise EdlError("camera_gap", "камера снимала не весь этот участок")
    if layout == "broll":
        path = clips_dir / params["clip"] if clips_dir is not None else None
        if path is None or not path.is_file():
            raise EdlError("no_clip", f"клипа {params['clip']} нет в папке своих кадров (list_broll)")
        if params["at"] >= clip_duration(path):
            raise EdlError("bad_params", "at: клип короче — с этой секунды показать нечего")
    vid = _id("vis")
    with write(conn):
        clash = conn.execute(
            "SELECT id FROM visual_segments WHERE project_id = ? AND asset_id = ? AND review != 'reverted' "
            "AND src_start_us < ? AND src_end_us > ?", (pid, asset_id, src_end_us, src_start_us)).fetchone()
        if clash:
            raise Conflict("visual_overlap", f"участок пересекается с другим ({clash[0]}): сначала верните тот")
        conn.execute(
            "INSERT INTO visual_segments (id, project_id, asset_id, src_start_us, src_end_us, layout, params_json, "
            "goal, created_by, review, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, pid, asset_id, src_start_us, src_end_us, layout, json.dumps(params), goal.strip()[:500], actor,
             "kept" if actor == "user" else "pending", _now(), _now() if actor == "user" else None))
    return _row(_one(conn, "SELECT * FROM visual_segments WHERE id = ?", vid))


def review_segment(conn: sqlite3.Connection, pid: str, vid: str, action: str, actor: str) -> dict:
    """Автор: keep — оставить, revert — вернуть как было (участок перестаёт действовать)."""
    _only_user(actor, "проверять раскладку")
    if action not in ("keep", "revert"):
        raise EdlError("bad_action", "keep или revert")
    with write(conn):
        _one(conn, "SELECT id FROM visual_segments WHERE id = ? AND project_id = ?", vid, pid)
        conn.execute("UPDATE visual_segments SET review = ?, decided_at = ? WHERE id = ?",
                     ("kept" if action == "keep" else "reverted", _now(), vid))
    return _row(_one(conn, "SELECT * FROM visual_segments WHERE id = ?", vid))


def update_params(conn: sqlite3.Connection, pid: str, vid: str, params: dict, actor: str) -> dict:
    """Автор поправил участок на мониторе (положение окна лица, рамки экрана):
    новые параметры той же раскладки; участок считается проверенным им."""
    _only_user(actor, "править участок кадра")
    with write(conn):
        row = _one(conn, "SELECT * FROM visual_segments WHERE id = ? AND project_id = ?", vid, pid)
        clean = validate_params(row["layout"], params)
        conn.execute("UPDATE visual_segments SET params_json = ?, review = 'kept', decided_at = ? WHERE id = ?",
                     (json.dumps(clean), _now(), vid))
    return _row(_one(conn, "SELECT * FROM visual_segments WHERE id = ?", vid))


def active_segments(conn: sqlite3.Connection, pid: str) -> list[dict]:
    return [_row(r) for r in conn.execute(
        "SELECT * FROM visual_segments WHERE project_id = ? AND review != 'reverted' ORDER BY asset_id, src_start_us",
        (pid,))]


def list_segments(conn: sqlite3.Connection, pid: str, edl: CompiledEdl | None = None) -> list[dict]:
    """Все участки (и возвращённые) с местом в версии: кадры ролика, где он виден."""
    out = []
    for r in conn.execute("SELECT * FROM visual_segments WHERE project_id = ? ORDER BY created_at", (pid,)):
        d = _row(r)
        d["edited_ranges"] = _edited_ranges(edl, d) if edl else []
        d["in_revision"] = bool(d["edited_ranges"])
        out.append(d)
    return out


def _edited_ranges(edl: CompiledEdl, seg: dict) -> list[tuple[int, int]]:
    if seg["asset_id"] not in edl.sources:
        return []
    fps = edl.sources[seg["asset_id"]].fps
    a, b = us_to_frame(seg["src_start_us"], fps), us_to_frame(seg["src_end_us"], fps)
    out = []
    for c in edl.clips:
        if c.asset_id != seg["asset_id"]:
            continue
        lo, hi = max(a, c.src_start_frame), min(b, c.src_end_frame)
        if lo < hi:
            out.append((c.out_start_frame + lo - c.src_start_frame, c.out_start_frame + hi - c.src_start_frame))
    return out


def _shot(start: int, end: int, spec: dict, cuts: tuple[int, ...] = (), clips_dir: Path | None = None,
          bg: Path | None = None, fps: Fraction | None = None) -> Shot:
    z, p, r, alt = spec.get("zoom"), spec.get("punch"), spec.get("region"), spec.get("alternate")
    f, c = spec.get("face"), spec.get("card")
    return Shot(start, end, spec["layout"],
                zoom=(z["x"], z["y"], z["w"], z["h"]) if z else None,
                punch=(p["scale"], p["cx"], p["cy"]) if p else None,
                pip_slot=spec.get("slot", DEFAULT_PIP["slot"]), pip_size=spec.get("size", DEFAULT_PIP["size"]),
                region=(r["x"], r["y"], r["w"], r["h"]) if r else None,
                face_cx=spec.get("face_cx", 0.5), enter=spec.get("enter"),
                punch_release=p.get("release") if p else None, punch_attack=p.get("attack") if p else None,
                push=spec.get("push"),
                alternate=(alt["step"], alt["cx"], alt["cy"]) if alt else None, cuts=cuts,
                drop=spec.get("drop"),
                clip=str(clips_dir / spec["clip"]) if spec.get("clip") and clips_dir is not None else None,
                clip_at=spec.get("at", 0.0),
                bg=str(bg) if bg is not None and spec["layout"] == "side" else None,
                bg_at=float(start / fps) if bg is not None and fps else 0.0,
                face_box=(f["x"], f["y"], f["size"]) if f else None,
                card_box=(c["x"], c["y"], c["w"]) if c else None)


def _jump_cuts(edl: CompiledEdl, start: int, end: int) -> tuple[int, ...]:
    """Склейки внутри [start, end) — кадры от start. Куски, идущие в исходнике
    подряд, склейкой не считаются: картинка там не прыгает."""
    out = []
    for prev, c in zip(edl.clips, edl.clips[1:]):
        if start < c.out_start_frame < end and not (
                prev.asset_id == c.asset_id and prev.src_end_frame == c.src_start_frame):
            out.append(c.out_start_frame - start)
    return tuple(out)


def build_plan(conn: sqlite3.Connection, pid: str, edl: CompiledEdl, width: int, height: int,
               clips_dir: Path | None = None, bg_dir: Path | None = None) -> VisualPlan | None:
    """План раскладки для ролика по монтажному листу. None — экран как есть."""
    default = project_layout(conn, pid)
    # Видео-фон проекта — за рамкой экрана на всех участках side (и своих тоже)
    bg = bg_dir / default["bg"] if default.get("bg") and bg_dir is not None else None
    bg = bg if bg is not None and bg.is_file() else None
    segs = active_segments(conn, pid)
    cams = cameras(conn, pid)
    if default["layout"] == "screen" and not segs:
        return None
    by_asset: dict[str, list[dict]] = {}
    for s in segs:
        by_asset.setdefault(s["asset_id"], []).append(s)
    pieces: list[tuple[int, int, dict]] = []
    for c in edl.clips:
        fps = edl.sources[c.asset_id].fps
        cuts = {c.src_start_frame, c.src_end_frame}
        own = by_asset.get(c.asset_id, [])
        for s in own:
            for f in (us_to_frame(s["src_start_us"], fps), us_to_frame(s["src_end_us"], fps)):
                if c.src_start_frame < f < c.src_end_frame:
                    cuts.add(f)
        marks = sorted(cuts)
        for f0, f1 in zip(marks, marks[1:]):
            mid_us = frame_to_us(f0, fps) + (frame_to_us(f1, fps) - frame_to_us(f0, fps)) // 2
            seg = next((s for s in own if s["src_start_us"] <= mid_us < s["src_end_us"]), None)
            spec = {"layout": seg["layout"], **seg["params"]} if seg else dict(default)
            if spec["layout"] == "broll" and (clips_dir is None or not (clips_dir / spec["clip"]).is_file()):
                spec = dict(default)  # клипа нет (стёрт) — как вне участков
            if spec["layout"] in ("camera", "pip", "side"):
                cam = cams.get(c.asset_id)
                if cam is None or not camera_covers(conn, cam, frame_to_us(f0, fps), frame_to_us(f1, fps)):
                    spec = {"layout": "screen"}  # камеры здесь нет — показываем экран
            o0 = c.out_start_frame + f0 - c.src_start_frame
            pieces.append((o0, o0 + f1 - f0, spec, seg["id"] if seg else None))
    shots: list[tuple[int, int, dict]] = []
    last_seg = None
    for a, b, spec, seg_id in pieces:
        # Соседние куски с одной раскладкой — один участок; но зум и укрупнение
        # с возвратом начинаются заново на каждом своём участке: такие не склеиваем
        pch = spec.get("punch", {})
        fresh = (bool(pch.get("release") or pch.get("attack")) or spec["layout"] == "broll") and seg_id != last_seg
        if shots and shots[-1][2] == spec and shots[-1][1] == a and not fresh:
            shots[-1] = (shots[-1][0], b, spec)
        else:
            shots.append((a, b, spec))
        last_seg = seg_id
    if all(spec == {"layout": "screen"} for *_, spec in shots):
        return None
    moves = lambda spec: spec.get("push") or spec.get("alternate")  # noqa: E731 — движение по склейкам
    return VisualPlan(width, height, tuple(_shot(a, b, spec, _jump_cuts(edl, a, b) if moves(spec) else (), clips_dir,
                                                 bg, edl.profile.fps)
                                           for a, b, spec in shots), cams)


def fingerprint(conn: sqlite3.Connection, pid: str) -> str:
    """Отпечаток всего, от чего зависит картинка: участки, раскладка проекта,
    синхронизация камер. Меняется — пробный ролик и экспорт собираются заново."""
    data = {
        "default": project_layout(conn, pid),
        "segments": [(s["asset_id"], s["src_start_us"], s["src_end_us"], s["layout"], s["params"])
                     for s in active_segments(conn, pid)],
        "cameras": {k: (v.asset_id, v.offset_us, v.drift_ppm) for k, v in cameras(conn, pid).items()},
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
