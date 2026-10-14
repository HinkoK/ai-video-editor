"""Проекты, исходники, версии, предложения правок и правила их утверждения.

Все правила здесь, на сервере. actor — кто просит: 'user' (автор через
приложение) или 'agent' (Claude через MCP). Права по actor проверяет и
api.py, и этот модуль: если маршрут сервера по ошибке открыт агенту,
утвердить правку он всё равно не сможет.
"""

import hashlib
import json
import sqlite3
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from longform.db import write
from longform.edl import MIN_CLIP_FRAMES, Clip, CompiledEdl, EdlError, OutputProfile, SourceInfo, compile_edl
from longform.ops import (
    MOVE_REASON,
    Move,
    Op,
    Protection,
    SnappedOp,
    apply_ops,
    build_clips,
    guard_slivers,
    kept_intervals,
    lands_next_to,
    neighbours,
    moved_protected,
    risk_flags,
    snap,
    validate_ops,
)
from longform.probe import UnsupportedMedia, probe
from longform.timebase import frame_at, frame_to_us, us_to_frame

ACTORS = ("user", "agent")
BRIEF_LANGUAGES = ("ru", "uk")


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Forbidden(PermissionError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _only_user(actor: str, what: str) -> None:
    if actor != "user":
        raise Forbidden(f"{what} может только автор")


def _one(conn: sqlite3.Connection, sql: str, *args) -> sqlite3.Row:
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise NotFound("не найдено")
    return row


# ---------- проекты и исходники ----------

def create_project(conn: sqlite3.Connection, name: str, actor: str) -> dict:
    _only_user(actor, "создать проект")
    if not name.strip():
        raise EdlError("no_name", "у проекта должно быть название")
    pid = _id("prj")
    with write(conn):
        conn.execute("INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)", (pid, name.strip(), _now()))
    return get_project(conn, pid)


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name, created_at, current_revision_id FROM projects ORDER BY created_at")
    return [dict(r) for r in rows]


def get_project(conn: sqlite3.Connection, pid: str) -> dict:
    p = dict(_one(conn, "SELECT * FROM projects WHERE id = ?", pid))
    p["assets"] = [_asset_dict(a) for a in conn.execute(
        "SELECT * FROM assets WHERE project_id = ? ORDER BY position", (pid,))]
    p["brief"] = get_brief(conn, pid)
    return p


def _asset_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    fps = Fraction(d["fps_num"], d["fps_den"])
    d["fps"] = f"{fps.numerator}/{fps.denominator}"
    d["duration_us"] = frame_to_us(d["frame_count"], fps)
    d["sync"] = json.loads(d.pop("sync_json")) if d.get("sync_json") else None
    return d


def set_sync(conn: sqlite3.Connection, pid: str, camera_id: str, main_id: str | None, state: str,
             offset_us: int | None, drift_ppm: float, report: dict) -> dict:
    """Итог синхронизации камеры (задача sync). Ручную подстройку автора не
    перетирает: пересчёт после неё — только по его кнопке «пересчитать»."""
    with write(conn):
        _one(conn, "SELECT id FROM assets WHERE id = ? AND project_id = ? AND role = 'camera'", camera_id, pid)
        conn.execute("UPDATE assets SET sync_to = ?, sync_offset_us = ?, sync_drift_ppm = ?, sync_state = ?, "
                     "sync_json = ? WHERE id = ?",
                     (main_id, offset_us, drift_ppm, state, json.dumps(report), camera_id))
    return _asset_dict(_one(conn, "SELECT * FROM assets WHERE id = ?", camera_id))


def nudge_sync(conn: sqlite3.Connection, pid: str, camera_id: str, delta_us: int, actor: str) -> dict:
    """Ручная подстройка синхронизации: сдвинуть камеру на delta_us."""
    _only_user(actor, "подстраивать синхронизацию камеры")
    if abs(delta_us) > 10_000_000:
        raise EdlError("bad_nudge", "подстройка — не больше 10 секунд за раз")
    with write(conn):
        row = _one(conn, "SELECT * FROM assets WHERE id = ? AND project_id = ? AND role = 'camera'", camera_id, pid)
        if row["sync_offset_us"] is None or row["sync_to"] is None:
            raise Conflict("not_synced", "камера ещё не сведена с записью экрана — сначала синхронизация")
        conn.execute("UPDATE assets SET sync_offset_us = ?, sync_state = 'manual' WHERE id = ?",
                     (row["sync_offset_us"] + delta_us, camera_id))
    return _asset_dict(_one(conn, "SELECT * FROM assets WHERE id = ?", camera_id))


def quick_fingerprint(path: Path) -> str:
    """Быстрый отпечаток: размер, первый и последний мегабайт.

    Это не полный хеш (на 10 ГБ он считался бы минуты). Его задача —
    заметить, что файл подменили или перезаписали, вместе с размером и
    временем изменения.
    """
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), 2)
            h.update(f.read())
    return h.hexdigest()[:32]


ROLES = ("main", "camera")


def register_asset(conn: sqlite3.Connection, pid: str, path_str: str, actor: str, role: str = "main") -> dict:
    """Подключить исходник по явному пути. Файл только читается и не копируется.

    role = 'main' — запись экрана с микрофоном: по ней расшифровка и правки,
    формат у всех таких файлов проекта один. role = 'camera' — камера,
    снятая одновременно: своя частота и размер, звук только для синхронизации."""
    _only_user(actor, "подключить файл")
    if role not in ROLES:
        raise EdlError("bad_role", "файл бывает записью экрана с микрофоном (main) или камерой (camera)")
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        raise EdlError("relative_path", "нужен полный путь к файлу")
    try:
        real = path.resolve(strict=True)  # ссылки раскрыты: храним настоящий путь
    except FileNotFoundError:
        raise EdlError("file_not_found", f"файл не найден: {path}") from None
    if not real.is_file():
        raise EdlError("not_a_file", f"это не файл: {real}")
    _one(conn, "SELECT id FROM projects WHERE id = ?", pid)
    # Медленное чтение файла — до транзакции, чтобы не держать базу
    info = probe(real)
    if info.audio is None:
        raise UnsupportedMedia(f"{real.name}: в файле нет звука, такие пока не поддерживаются")
    src = info.source_info("probe")  # отклонит плавающую частоту кадров
    stat = real.stat()
    fingerprint = quick_fingerprint(real)
    aid = _id("ast")
    with write(conn):
        p = _one(conn, "SELECT * FROM projects WHERE id = ?", pid)
        if conn.execute("SELECT 1 FROM assets WHERE project_id = ? AND path = ?", (pid, str(real))).fetchone():
            raise EdlError("duplicate_asset", f"файл уже подключён: {real.name}")
        fmt = (src.fps.numerator, src.fps.denominator, info.audio.sample_rate, info.audio.channels,
               info.video.width, info.video.height)
        if role == "camera":
            if not conn.execute("SELECT 1 FROM assets WHERE project_id = ? AND role = 'main'", (pid,)).fetchone():
                raise EdlError("no_main", "сначала подключите запись экрана с микрофоном: камера сводится с ней по звуку")
        elif p["fps_num"] is None:
            conn.execute(
                "UPDATE projects SET fps_num=?, fps_den=?, sample_rate=?, channels=?, width=?, height=? "
                "WHERE id = ?", (*fmt, pid))
        elif fmt != (p["fps_num"], p["fps_den"], p["sample_rate"], p["channels"], p["width"], p["height"]):
            raise EdlError(
                "format_mismatch",
                f"{real.name}: частота кадров, разрешение или звук не совпадают с первым файлом "
                "проекта. Приведение к одному формату появится, когда будут реальные файлы",
            )
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM assets WHERE project_id = ?", (pid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO assets (id, project_id, position, path, size_bytes, mtime_ns, fingerprint, "
            "fps_num, fps_den, frame_count, sample_rate, channels, audio_offset_num, audio_offset_den, "
            "width, height, registered_at, role, sync_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, pid, position, str(real), stat.st_size, stat.st_mtime_ns, fingerprint,
             src.fps.numerator, src.fps.denominator, src.frame_count, info.audio.sample_rate,
             info.audio.channels, src.audio_offset.numerator, src.audio_offset.denominator,
             info.video.width, info.video.height, _now(), role, "pending" if role == "camera" else None))
    return _asset_dict(_one(conn, "SELECT * FROM assets WHERE id = ?", aid))


def load_sources(conn: sqlite3.Connection, pid: str) -> tuple[dict[str, SourceInfo], list[str], OutputProfile]:
    p = _one(conn, "SELECT * FROM projects WHERE id = ?", pid)
    sources, order = {}, []
    for a in conn.execute("SELECT * FROM assets WHERE project_id = ? ORDER BY position", (pid,)):
        sources[a["id"]] = SourceInfo(
            asset_id=a["id"],
            fps=Fraction(a["fps_num"], a["fps_den"]),
            frame_count=a["frame_count"],
            sample_rate=a["sample_rate"],
            channels=a["channels"],
            audio_offset=Fraction(a["audio_offset_num"], a["audio_offset_den"]),
        )
        order.append(a["id"])
    if not order:
        raise EdlError("no_assets", "в проекте ещё нет исходников")
    return sources, order, OutputProfile(Fraction(p["fps_num"], p["fps_den"]), p["sample_rate"])


# ---------- версии ----------

def _clips_json(clips: list[Clip]) -> str:
    return json.dumps([{"id": c.id, "asset_id": c.asset_id, "src_start_us": c.src_start_us,
                        "src_end_us": c.src_end_us} for c in clips])


def _clips(row: sqlite3.Row) -> list[Clip]:
    return [Clip(c["id"], c["asset_id"], c["src_start_us"], c["src_end_us"]) for c in json.loads(row["clips_json"])]


def _moves(row: sqlite3.Row) -> list[Move]:
    """Слой перестановок версии; у версий до него — пустой."""
    return [Move.from_dict(m) for m in json.loads(row["moves_json"] or "[]")]


def _insert_revision(conn, pid, clips, state, parent_id, description, actor, moves=()) -> str:
    rid = _id("rev")
    number = conn.execute(
        "SELECT COALESCE(MAX(number), 0) + 1 FROM revisions WHERE project_id = ?", (pid,)).fetchone()[0]
    conn.execute(
        "INSERT INTO revisions (id, project_id, number, parent_id, state, clips_json, moves_json, description, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, pid, number, parent_id, state, _clips_json(clips), json.dumps([m.as_dict() for m in moves]),
         description, actor, _now()))
    return rid


def create_initial_revision(conn: sqlite3.Connection, pid: str, actor: str) -> dict:
    """Первая версия: все записи экрана целиком, в порядке подключения.
    Камеры в монтажный лист не входят: их картинка — слой кадра."""
    _only_user(actor, "создать первую версию")
    sources, order, profile = load_sources(conn, pid)
    mains = {r[0] for r in conn.execute("SELECT id FROM assets WHERE project_id = ? AND role = 'main'", (pid,))}
    order = [a for a in order if a in mains]
    clips = build_clips({a: [(0, sources[a].frame_count)] for a in order}, sources, order)
    compile_edl(clips, sources, profile)
    with write(conn):
        if _one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]:
            raise Conflict("has_revision", "у проекта уже есть версии")
        rid = _insert_revision(conn, pid, clips, "approved", None, "исходники целиком", actor)
        conn.execute("UPDATE projects SET current_revision_id = ? WHERE id = ?", (rid, pid))
    return get_revision(conn, pid, rid)


def compile_revision(conn: sqlite3.Connection, pid: str, rid: str) -> CompiledEdl:
    row = _one(conn, "SELECT * FROM revisions WHERE id = ? AND project_id = ?", rid, pid)
    sources, _, profile = load_sources(conn, pid)
    return compile_edl(_clips(row), sources, profile)


def get_revision(conn: sqlite3.Connection, pid: str, rid: str) -> dict:
    row = _one(conn, "SELECT * FROM revisions WHERE id = ? AND project_id = ?", rid, pid)
    edl = compile_revision(conn, pid, rid)
    fps = edl.profile.fps
    d = {k: row[k] for k in ("id", "number", "parent_id", "state", "description", "created_by", "created_at")}
    d["fps"] = f"{fps.numerator}/{fps.denominator}"
    d["total_frames"] = edl.total_frames
    d["duration_us"] = frame_to_us(edl.total_frames, fps)
    d["clips"] = [
        {"id": c.clip_id, "asset_id": c.asset_id,
         "src_start_us": frame_to_us(c.src_start_frame, fps), "src_end_us": frame_to_us(c.src_end_frame, fps),
         "out_start_frame": c.out_start_frame, "out_end_frame": c.out_end_frame}
        for c in edl.clips
    ]
    d["moves"] = move_places(edl, _moves(row))
    return d


def move_places(edl: CompiledEdl, moves: list[Move]) -> list[dict]:
    """Перестановки версии и где они в ролике: отрезок куска (кадры) и три
    стыка — вход куска, выход и место, откуда кусок взят (номера стыков как
    в joints и в preview_joint; None — стыка нет: кусок в начале или в конце
    ролика, всё вокруг вырезано или место занято другой перестановкой)."""
    js = joints(edl)
    at = {j["out_frame"]: i for i, j in enumerate(js)}
    out = []
    for m in moves:
        d = m.as_dict()
        src = edl.sources.get(m.asset_id)
        d.update(out_start_frame=None, out_end_frame=None, joints={"in": None, "out": None, "from": None})
        if src is None:
            out.append(d)
            continue
        a, b = us_to_frame(m.src_start_us, src.fps), us_to_frame(m.src_end_us, src.fps)
        own = [c for c in edl.clips if c.asset_id == m.asset_id]
        # Части куска — пересечения кусков ролика с отрезком: вырез рядом может
        # склеить кусок с соседом по записи, тогда кусок ролика шире отрезка
        parts = [(c.out_start_frame + max(a, c.src_start_frame) - c.src_start_frame,
                  c.out_start_frame + min(b, c.src_end_frame) - c.src_start_frame)
                 for c in own if c.src_start_frame < b and a < c.src_end_frame]
        if parts:
            start, end = min(x for x, _ in parts), max(y for _, y in parts)
            d.update(out_start_frame=start, out_end_frame=end)

            def out_of(f: int) -> int | None:
                c = next((c for c in own if c.src_start_frame <= f < c.src_end_frame), None)
                return None if c is None else c.out_start_frame + f - c.src_start_frame
            # Место, откуда взят: последний оставленный кадр до куска и первый после — встык в ролике
            before = max((min(c.src_end_frame, a) - 1 for c in own if c.src_start_frame < a), default=None)
            after = min((max(c.src_start_frame, b) for c in own if c.src_end_frame > b), default=None)
            p_out = out_of(before) if before is not None else None
            n_out = out_of(after) if after is not None else None
            d["joints"] = {"in": at.get(start), "out": at.get(end),
                           "from": at.get(n_out) if p_out is not None and n_out is not None
                           and p_out + 1 == n_out else None}
        out.append(d)
    return out


def list_revisions(conn: sqlite3.Connection, pid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, number, parent_id, state, description, created_by, created_at "
        "FROM revisions WHERE project_id = ? ORDER BY number", (pid,))
    return [dict(r) for r in rows]


def _mark_stale(conn: sqlite3.Connection, pid: str, current: str) -> None:
    """Предложения к старой версии устаревают, их кандидаты отбрасываются."""
    stale = conn.execute(
        "SELECT id, candidate_revision_id FROM proposals "
        "WHERE project_id = ? AND status = 'open' AND base_revision_id != ?", (pid, current)).fetchall()
    for s in stale:
        conn.execute("UPDATE proposals SET status = 'stale', decided_at = ? WHERE id = ?", (_now(), s["id"]))
        conn.execute("UPDATE revisions SET state = 'discarded' WHERE id = ? AND state = 'candidate'",
                     (s["candidate_revision_id"],))


def _set_current(conn: sqlite3.Connection, pid: str, expected: str, new: str) -> None:
    """Оптимистичная блокировка: сменить текущую версию, только если она всё ещё expected."""
    cur = conn.execute("UPDATE projects SET current_revision_id = ? WHERE id = ? AND current_revision_id = ?",
                       (new, pid, expected))
    if cur.rowcount != 1:
        raise Conflict("stale_base", "текущая версия уже сменилась, обновите и посмотрите заново")
    _mark_stale(conn, pid, new)


def revert(conn: sqlite3.Connection, pid: str, target_rid: str, actor: str) -> dict:
    """Откат — это новая версия с содержимым старой. История не теряется."""
    _only_user(actor, "откатить версию")
    with write(conn):
        target = _one(conn, "SELECT * FROM revisions WHERE id = ? AND project_id = ?", target_rid, pid)
        if target["state"] != "approved":
            raise EdlError("not_approved", "откатиться можно только к утверждённой версии")
        current = _one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        rid = _insert_revision(conn, pid, _clips(target), "approved", current,
                               f"возврат к версии {target['number']}", actor, _moves(target))
        _set_current(conn, pid, current, rid)
    return get_revision(conn, pid, rid)


# ---------- предложения правок ----------

def _protections(conn: sqlite3.Connection, pid: str) -> list[Protection]:
    rows = conn.execute("SELECT * FROM protected_ranges WHERE project_id = ? AND removed_at IS NULL", (pid,))
    return [Protection(r["asset_id"], r["src_start_us"], r["src_end_us"], r["kind"]) for r in rows]


def _op_from_row(r: sqlite3.Row) -> Op:
    alt = json.loads(r["kept_alt_json"]) if r["kept_alt_json"] else None
    target = (r["target_asset_id"], r["target_src_us"], r["target_side"]) if r["kind"] == "move" else None
    return Op(r["kind"], r["asset_id"], r["src_start_us"], r["src_end_us"], r["reason"], r["why"],
              tuple(alt) if alt else None, tuple(json.loads(r["segment_ids_json"])), target=target,
              undo_of=r["undo_of"])


def create_proposal(
    conn: sqlite3.Connection, pid: str, *, base_revision_id: str, pass_: str, rationale: str,
    policy_version: str, ops: list[Op], actor: str, align: bool = True,
) -> dict:
    if actor not in ACTORS:
        raise Forbidden("неизвестный автор")
    if pass_ != "cleanup":
        raise EdlError("pass_not_allowed", "в режиме бережной чистки доступен только проход cleanup")
    if not ops:
        raise EdlError("no_ops", "в предложении нет правок")
    if not rationale.strip():
        raise EdlError("no_rationale", "у предложения должно быть общее объяснение")
    if len(ops) > 1 and any(o.kind in ("move", "unmove") for o in ops):
        raise EdlError("move_alone", "перестановка — отдельным предложением: одна перестановка, без вырезов "
                                     "и возвратов")
    sources, order, profile = load_sources(conn, pid)
    cameras = {r[0] for r in conn.execute("SELECT id FROM assets WHERE project_id = ? AND role = 'camera'", (pid,))}
    if any(o.asset_id in cameras or (o.target and o.target[0] in cameras) for o in ops):
        raise EdlError("camera_op", "камеру не режут отдельно: правки идут по записи экрана с микрофоном, "
                                    "камера следует за ней по синхронизации")
    order = [a for a in order if a not in cameras]
    if align:
        edges = {}
        if any(o.kind == "move" for o in ops):
            row = conn.execute("SELECT * FROM revisions WHERE id = ? AND project_id = ?",
                               (base_revision_id, pid)).fetchone()
            edges = _move_edges(_clips(row), _moves(row), sources) if row is not None else {}
        adjusted, adjust_notes = _align_to_silence(conn, ops, edges, sources)
    else:  # явная ручная правка автора: граница ровно там, где сказано
        adjusted, adjust_notes = list(ops), ["граница задана вручную"] * len(ops)
    with write(conn):
        project = _one(conn, "SELECT * FROM projects WHERE id = ?", pid)
        if project["current_revision_id"] != base_revision_id:
            raise Conflict("stale_base", "предложение сделано к устаревшей версии, возьмите текущую")
        base_row = _one(conn, "SELECT * FROM revisions WHERE id = ?", base_revision_id)
        base, base_moves = _clips(base_row), _moves(base_row)
        # id правок — заранее: перестановка записывается в слой версии по id своей правки
        op_ids = [_id("op") for _ in adjusted]
        snapped = [snap(o, sources, op_id=op_ids[i]) for i, o in enumerate(adjusted)]
        # Огрызок у стыка уходит в саму правку: «вернуть» потом откатит её ровно
        snapped, guard_notes = guard_slivers(snapped, kept_intervals(base, sources), base_moves, sources, order)
        adjust_notes = ["; ".join(n for n in (adjust_notes[i], guard_notes[i]) if n) or None
                        for i in range(len(snapped))]
        validate_ops(snapped, kept_intervals(base, sources), base_moves)
        applied = apply_ops(base, snapped, sources, order, is_quiet=_quiet_check(conn), moves=base_moves)
        compile_edl(applied.clips, sources, profile)
        protections = _protections(conn, pid)
        number = conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM proposals WHERE project_id = ?", (pid,)).fetchone()[0]
        cand = _insert_revision(conn, pid, applied.clips, "candidate", base_revision_id,
                                f"кандидат из предложения {number}", actor, applied.moves)
        prop_id = _id("prp")
        conn.execute(
            "INSERT INTO proposals (id, project_id, number, base_revision_id, candidate_revision_id, pass, "
            "rationale, policy_version, notes_json, status, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
            (prop_id, pid, number, base_revision_id, cand, pass_, rationale.strip(), policy_version,
             json.dumps(applied.notes, ensure_ascii=False), actor, _now()))
        language = _brief_language(conn, pid)
        for i, s in enumerate(snapped):
            flags = risk_flags(s, protections)
            if s.op.kind == "move":
                from longform import graphics

                # Графика на границе куска или месте режется: части рисуются отдельно
                if graphics.split_by(conn, pid, s.op.asset_id, [s.start_us, s.end_us, s.target_us]):
                    flags.append("splits_graphic")
            flags, extra = _speech_flags(conn, s, flags, language)
            note = "; ".join(n for n in (adjust_notes[i], extra) if n) or None
            _insert_op(conn, prop_id, i, s, flags, ops[i], note, op_ids[i])
    if actor == "agent" and project["agent_mode"] == "apply":
        _auto_apply(conn, pid, prop_id)
    return get_proposal(conn, pid, prop_id)


def _quiet_check(conn: sqlite3.Connection):
    """Есть ли речь в интервале — по громкости; без анализа считаем, что есть."""
    from longform import asset_data

    def is_quiet(asset: str, start_us: int, end_us: int) -> bool:
        loud = asset_data.loudness(conn, asset)
        return loud is not None and loud.speech_span(start_us, end_us) is None
    return is_quiet


def _brief_language(conn: sqlite3.Connection, pid: str) -> str:
    active = get_brief(conn, pid)["active"]
    return (active or {}).get("data", {}).get("language") or "ru"


# Перестановка: граница куска или место ближе этого к краю оставленного
# интервала (стыку) и без речи между ними — встаёт на стык. Иначе на старом
# или новом месте остаётся тихий огрызок в несколько кадров (а короче двух
# кадров compile_edl его и не примет)
MOVE_SNAP_US = 300_000


def _move_edges(clips: list[Clip], moves: list[Move], sources: dict[str, SourceInfo]) -> dict[str, list[int]]:
    """Где раскладка версии уже режет запись: края оставленных интервалов и
    границы прежних перестановок (мкс исходника по файлам)."""
    edges: dict[str, set[int]] = {}
    for asset, ivs in kept_intervals(clips, sources).items():
        fps = sources[asset].fps
        edges[asset] = {frame_to_us(x, fps) for iv in ivs for x in iv}
    for m in moves:
        edges.setdefault(m.asset_id, set()).update((m.src_start_us, m.src_end_us))
        edges.setdefault(m.target_asset_id, set()).add(m.target_src_us)
    return {a: sorted(e) for a, e in edges.items()}


def _align_move(o: Op, loud, edges: list[int], fps: Fraction) -> tuple[Op, str | None]:
    """Границы перестановки. Начало куска — перед речью с запасом (keep_after),
    конец — после речи (keep_before); место «после» — после речи, «перед» —
    перед речью. Точная (ручная правка автора) — граница в ближайший тихий
    момент. Потом подтяжка к стыкам (MOVE_SNAP_US); у точной — только
    защита от огрызка короче двух кадров."""
    t_asset, p, side = o.target
    a, b = o.src_start_us, o.src_end_us
    parts: list[str] = []
    sliver_us = frame_to_us(MIN_CLIP_FRAMES, fps)

    def near_edge(x: int, radius: int, need_quiet: bool) -> int | None:
        best = None
        for e in edges:
            d = abs(e - x)
            if d == 0:
                return e
            if d >= radius or (best is not None and d >= abs(best - x)):
                continue
            lo, hi = min(e, x), max(e, x)
            if need_quiet and (loud is None or loud.speech_near((lo + hi) // 2, (hi - lo) // 2)):
                continue
            best = e
        return best

    def place(x: int, keep, name: str) -> int:
        edge = near_edge(x, sliver_us, need_quiet=False)
        if o.precise:
            if edge is not None:
                return edge
            y = loud.precise_boundary(x) if loud is not None else x
        else:
            y, why = keep(x) if loud is not None else (x, None)
            if why:
                parts.append(f"{name}: {why}")
        edge = near_edge(y, sliver_us, need_quiet=False)
        if edge is None and not o.precise:
            edge = near_edge(y, MOVE_SNAP_US, need_quiet=True)
        if edge is not None and edge != y:
            parts.append(f"{name}: подтянуто к стыку ({(edge - y) // 1000:+d} мс)")
            y = edge
        return y

    a2 = place(a, loud.keep_after if loud is not None else None, "начало")
    b2 = place(b, loud.keep_before if loud is not None else None, "конец")
    p2 = place(p, (loud.keep_before if side == "after" else loud.keep_after) if loud is not None else None, "место")
    if b2 <= a2:
        raise EdlError("vanished", "после выравнивания от куска ничего не осталось")
    if loud is None:
        parts.append("громкость исходника ещё не измерена, граница не выровнена по тишине")
    if (a2, b2, p2) != (a, b, p):
        parts.append(f"сдвиг: начало {(a2 - a) // 1000:+d}, конец {(b2 - b) // 1000:+d}, "
                     f"место {(p2 - p) // 1000:+d} мс")
    return replace(o, src_start_us=a2, src_end_us=b2, target=(t_asset, p2, side)), "; ".join(parts) or None


def _align_to_silence(conn: sqlite3.Connection, ops: list[Op], edges: dict[str, list[int]] | None = None,
                      sources: dict[str, SourceInfo] | None = None) -> tuple[list[Op], list[str | None]]:
    """Границы правок — в тишину, с запасом от речи (analysis.Loudness).

    Если громкость исходника ещё не измерена, границы остаются как есть,
    а в правке остаётся пометка об этом. У перестановки — ещё и подтяжка
    к стыкам версии (edges — _move_edges).
    """
    from longform import asset_data

    out, notes = [], []
    for i, o in enumerate(ops):
        loud = asset_data.loudness(conn, o.asset_id)
        if o.kind == "unmove":
            out.append(o)
            notes.append(None)
            continue
        if o.kind == "move":
            if o.target is None or sources is None or o.asset_id not in sources:
                out.append(o)  # ошибку назовёт snap
                notes.append(None)
                continue
            moved, note = _align_move(o, loud, (edges or {}).get(o.asset_id, []), sources[o.asset_id].fps)
            out.append(moved)
            notes.append(note)
            continue
        if loud is None:
            out.append(o)
            notes.append("громкость исходника ещё не измерена, граница не выровнена по тишине")
            continue
        if o.precise:
            a, b = loud.precise_boundary(o.src_start_us), loud.precise_boundary(o.src_end_us)
            if b <= a:
                raise EdlError("vanished", f"правка {i}: после выравнивания от неё ничего не осталось")
            out.append(replace(o, src_start_us=a, src_end_us=b))
            notes.append(f"точная правка: границы в ближайшие тихие моменты "
                         f"({(a - o.src_start_us) // 1000:+d} и {(b - o.src_end_us) // 1000:+d} мс)"
                         if (a, b) != (o.src_start_us, o.src_end_us) else "точная правка: границы в тишине, как заданы")
            continue
        if o.kind == "remove":
            a, na = loud.keep_before(o.src_start_us)
            b, nb = loud.keep_after(o.src_end_us)
        else:  # у возврата начало — граница перед оставляемым, конец — после
            a, na = loud.keep_after(o.src_start_us)
            b, nb = loud.keep_before(o.src_end_us)
        if b <= a:
            raise EdlError(
                "vanished",
                f"правка {i}: диапазон целиком в паузе у речи, а у речи всегда остаётся запас тишины, "
                "поэтому вырезать нечего. Чтобы убрать паузу или участок без звука, укажи её из list_silences")
        parts = [f"начало: {na}" if na else None, f"конец: {nb}" if nb else None]
        if (a, b) != (o.src_start_us, o.src_end_us):
            parts.append(f"границы сдвинуты на {(a - o.src_start_us) // 1000:+d} и {(b - o.src_end_us) // 1000:+d} мс")
        out.append(replace(o, src_start_us=a, src_end_us=b))
        notes.append("; ".join(p for p in parts if p) or None)
    return out, notes


def _speech_flags(conn: sqlite3.Connection, s: SnappedOp, flags: list[str], language: str) -> tuple[list[str], str | None]:
    """Пометки, которым нужны громкость и текст: обрезанное слово и
    важные слова, сохранённые в оставленном дубле."""
    from longform import asset_data
    from longform.transcribe import markers_preserved

    note = None
    loud = asset_data.loudness(conn, s.op.asset_id)
    # Край файла — не стык: по ту сторону ничего не остаётся. У перестановки
    # три границы: начало и конец куска и место, куда он встаёт
    bounds = (s.start_us, s.end_us, s.target_us) if s.op.kind == "move" else (s.start_us, s.end_us)
    edges = [b for b in bounds if loud is not None and b is not None and not loud._outside(b)]
    if loud and any(loud.speech_near(b) for b in edges):
        flags = [*flags, "cut_in_speech"]
    alt = s.op.kept_alternative
    if "high_impact" in flags and alt and s.op.reason in ("failed_take", "false_start"):
        removed = asset_data.text_in_range(conn, s.op.asset_id, s.start_us, s.end_us, any_overlap=True)
        kept = asset_data.text_in_range(conn, alt[0], alt[1], alt[2], any_overlap=True)
        if markers_preserved(removed, kept, language):
            flags = [f for f in flags if f != "high_impact"]
            note = "оставленный дубль повторяет вырезанный, его оговорки и числа сохранены"
    return flags, note


def _insert_op(conn, prop_id: str, position: int, s: SnappedOp, flags: list[str], requested: Op,
               note: str | None, op_id: str | None = None) -> None:
    target = s.op.target if s.op.kind == "move" else None
    conn.execute(
        "INSERT INTO proposal_ops (id, proposal_id, position, kind, asset_id, src_start_us, src_end_us, "
        "reason, why, kept_alt_json, segment_ids_json, risk_flags_json, requested_start_us, requested_end_us, "
        "adjust_note, target_asset_id, target_src_us, target_side, undo_of) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_id or _id("op"), prop_id, position, s.op.kind, s.op.asset_id, s.start_us, s.end_us, s.op.reason,
         s.op.why.strip(), json.dumps(list(s.op.kept_alternative)) if s.op.kept_alternative else None,
         json.dumps(list(s.op.segment_ids)), json.dumps(flags), requested.src_start_us, requested.src_end_us,
         note, target[0] if target else None, s.target_us if target else None, target[2] if target else None,
         s.op.undo_of if s.op.kind == "unmove" else None))


def joints(edl: CompiledEdl) -> list[dict]:
    """Стыки версии: где в ролике (кадр) и какой кусок исходника между ними вырезан.

    kind: 'cut' — куски стоят рядом и в раскладке по исходнику (между ними
    только вырезанное); 'move' — стык перестановки: вход переставленного
    куска, выход из него или место, откуда кусок взят."""
    order = {a: i for i, a in enumerate(edl.sources)}
    ranked = sorted(range(len(edl.clips)),
                    key=lambda i: (order.get(edl.clips[i].asset_id, 0), edl.clips[i].src_start_frame))
    rank = {i: r for r, i in enumerate(ranked)}
    out = []
    for k, (c, d) in enumerate(zip(edl.clips, edl.clips[1:])):
        if c.asset_id == d.asset_id and c.src_end_frame == d.src_start_frame:
            continue
        fps_c, fps_d = edl.sources[c.asset_id].fps, edl.sources[d.asset_id].fps
        out.append({"out_frame": d.out_start_frame,
                    "before_asset": c.asset_id, "before_src_end_us": frame_to_us(c.src_end_frame, fps_c),
                    "after_asset": d.asset_id, "after_src_start_us": frame_to_us(d.src_start_frame, fps_d),
                    "kind": "cut" if rank[k + 1] == rank[k] + 1 else "move"})
    return out


def _current_flags(conn: sqlite3.Connection, pid: str, row: sqlite3.Row) -> list[str]:
    """Пометки правки сейчас: сохранённые плюс жёсткая защита, поставленная
    автором уже после создания предложения. Иначе защита, добавленная к
    открытому предложению, не остановила бы его утверждение пакетом."""
    flags = json.loads(row["risk_flags_json"])
    if row["kind"] == "remove" and "protected" not in flags:
        hard = [p for p in _protections(conn, pid) if p.kind == "hard" and p.asset_id == row["asset_id"]]
        if any(p.src_start_us < row["src_end_us"] and row["src_start_us"] < p.src_end_us for p in hard):
            flags = ["protected", *flags]
    if row["kind"] == "move" and "protected" not in flags and moved_protected(
            row["asset_id"], row["src_start_us"], row["src_end_us"], row["target_src_us"], _protections(conn, pid)):
        flags = ["protected", *flags]
    return flags


def get_proposal(conn: sqlite3.Connection, pid: str, prop_id: str) -> dict:
    p = dict(_one(conn, "SELECT * FROM proposals WHERE id = ? AND project_id = ?", prop_id, pid))
    p["notes"] = json.loads(p.pop("notes_json"))

    sources, order, _ = load_sources(conn, pid)
    mains = [a for a in order if a in {r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE project_id = ? AND role = 'main'", (pid,))}]

    def placed(rid: str | None) -> dict:
        """Стыки версии, где в ней перестановки, и как найти стык выреза."""
        if not rid:
            return {"joints": [], "moves": [], "edl": None, "layer": []}
        edl = compile_revision(conn, pid, rid)
        row = _one(conn, "SELECT clips_json, moves_json FROM revisions WHERE id = ?", rid)
        layer = _moves(row)
        return {"joints": joints(edl), "moves": move_places(edl, layer), "edl": edl, "layer": layer,
                "kept": kept_intervals(_clips(row), sources)}

    cand = placed(p["candidate_revision_id"])
    current = conn.execute("SELECT current_revision_id FROM projects WHERE id = ?", (pid,)).fetchone()[0]
    cur = placed(current)
    p["ops"] = []
    for r in conn.execute("SELECT * FROM proposal_ops WHERE proposal_id = ? ORDER BY position", (prop_id,)):
        op = dict(r)
        op["kept_alternative"] = json.loads(op.pop("kept_alt_json")) if r["kept_alt_json"] else None
        op["segment_ids"] = json.loads(op.pop("segment_ids_json"))
        op.pop("risk_flags_json")
        op["risk_flags"] = _current_flags(conn, pid, r) if p["status"] == "open" else json.loads(r["risk_flags_json"])
        op["target"] = ({"asset_id": op["target_asset_id"], "src_us": op["target_src_us"], "side": op["target_side"]}
                        if op["kind"] == "move" else None)

        # Стык, внутри выреза которого лежит эта правка: туда смотреть «после».
        # В кандидате — до решения; в текущей версии — при проверке после
        # применения. Стыки перестановок не в счёт; у самой перестановки —
        # вход переставленного куска (нет входа — выход)
        def joint_of(v):
            js = v["joints"]
            if op["kind"] == "move":
                m = next((x for x in v["moves"] if x["op_id"] == op["id"]), None)
                if m is None:
                    return None
                k = m["joints"]["in"] if m["joints"]["in"] is not None else m["joints"]["out"]
                return js[k]["out_frame"] if k is not None else None
            if op["kind"] != "remove":
                return None
            if not v["layer"]:
                # Без перестановок — как раньше: стык, внутри выреза которого лежит правка
                return next((j["out_frame"] for j in js if j["before_asset"] == op["asset_id"] == j["after_asset"]
                             and j["before_src_end_us"] <= op["src_start_us"] + 40_000
                             and op["src_end_us"] - 40_000 <= j["after_src_start_us"]), None)
            # С перестановками — стык между теми, кто стоял бы вокруг вырезанного
            # (и у стыка перестановки: тишина на месте, откуда взят кусок)
            fps = sources[op["asset_id"]].fps
            a, b = us_to_frame(op["src_start_us"], fps), us_to_frame(op["src_end_us"], fps)
            prev, nxt = neighbours(v["kept"], v["layer"], sources, mains, op["asset_id"], a, b)
            if prev is None or nxt is None:
                return None
            return next((j["out_frame"] for j in js
                         if (j["before_asset"], us_to_frame(j["before_src_end_us"], sources[j["before_asset"]].fps))
                         == prev and (j["after_asset"],
                                      us_to_frame(j["after_src_start_us"], sources[j["after_asset"]].fps)) == nxt),
                        None)
        op["candidate_joint_frame"] = joint_of(cand)
        op["current_joint_frame"] = joint_of(cur)
        p["ops"].append(op)
    return p


def list_proposals(conn: sqlite3.Connection, pid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, number, status, base_revision_id, candidate_revision_id, result_revision_id, pass, "
        "rationale, created_by, created_at, decided_at, auto_applied, "
        "(SELECT COUNT(*) FROM proposal_ops o WHERE o.proposal_id = proposals.id AND o.decision = 'accepted' "
        " AND o.review = 'pending') AS unreviewed, "
        "(SELECT COUNT(*) FROM proposal_ops o WHERE o.proposal_id = proposals.id AND o.review = 'held') AS held, "
        "(SELECT COUNT(*) FROM proposal_ops o WHERE o.proposal_id = proposals.id) AS ops_count "
        "FROM proposals WHERE project_id = ? ORDER BY number",
        (pid,))
    return [dict(r) for r in rows]


def approve(
    conn: sqlite3.Connection, pid: str, prop_id: str, *, actor: str,
    exclude: list[str] = (), acknowledge: list[str] = (), overrides: dict[str, str] | None = None,
) -> dict:
    """Утвердить предложение целиком или всё, кроме exclude.

    Правки с пометками риска принимаются только поштучно: их id должны быть
    в acknowledge. Правка, задевающая жёсткую защиту, требует ещё и причину
    в overrides. Всё в одной транзакции.
    """
    _only_user(actor, "утвердить правки")
    overrides = overrides or {}
    with write(conn):
        prop = _one(conn, "SELECT * FROM proposals WHERE id = ? AND project_id = ?", prop_id, pid)
        if prop["status"] != "open":
            raise Conflict("not_open", f"предложение уже в состоянии {prop['status']}")
        current = _one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        if current != prop["base_revision_id"]:
            raise Conflict("stale_base", "предложение сделано к устаревшей версии")
        rows = conn.execute("SELECT * FROM proposal_ops WHERE proposal_id = ? ORDER BY position",
                            (prop_id,)).fetchall()
        ids = {r["id"] for r in rows}
        unknown = (set(exclude) | set(acknowledge) | set(overrides)) - ids
        if unknown:
            raise EdlError("unknown_op", f"в предложении нет правок {sorted(unknown)}")
        included = [r for r in rows if r["id"] not in set(exclude)]
        if not included:
            raise EdlError("nothing_to_approve", "все правки исключены, это отклонение предложения")
        flags = {r["id"]: _current_flags(conn, pid, r) for r in included}
        need_ack = [r["id"] for r in included if flags[r["id"]] and r["id"] not in acknowledge]
        need_reason = [r["id"] for r in included
                       if "protected" in flags[r["id"]] and not overrides.get(r["id"], "").strip()]
        if need_ack or need_reason:
            raise EdlError(
                "needs_review",
                "рискованные правки принимаются поштучно. Подтвердите отдельно: "
                f"{need_ack}; задевают защищённое и требуют причины: {need_reason}",
            )
        _commit_decision(conn, pid, prop, rows, {r["id"] for r in rows} - {r["id"] for r in included},
                         overrides, actor, current)
    return get_proposal(conn, pid, prop_id)


def _commit_decision(conn: sqlite3.Connection, pid: str, prop: sqlite3.Row, rows: list[sqlite3.Row],
                     excluded: set[str], overrides: dict[str, str], actor: str, current: str) -> str:
    """Применить предложение без исключённых правок и сделать результат
    текущей версией. Вызывается внутри транзакции записи — и когда автор
    утверждает, и когда сервер применяет правки агента в режиме «сразу»."""
    sources, order, profile = load_sources(conn, pid)
    included = [r for r in rows if r["id"] not in excluded]
    if len(included) == len(rows):
        result = prop["candidate_revision_id"]
        conn.execute("UPDATE revisions SET state = 'approved' WHERE id = ?", (result,))
    else:
        # Частичное принятие: новая версия из базы и принятых правок, проверяется
        # заново; слой перестановок базы сохраняется
        base_row = _one(conn, "SELECT * FROM revisions WHERE id = ?", prop["base_revision_id"])
        snapped = [snap(_op_from_row(r), sources, op_id=r["id"]) for r in included]
        applied = apply_ops(_clips(base_row), snapped, sources, order, is_quiet=_quiet_check(conn),
                            moves=_moves(base_row))
        compile_edl(applied.clips, sources, profile)
        result = _insert_revision(conn, pid, applied.clips, "approved", prop["base_revision_id"],
                                  f"часть предложения {prop['number']}", actor, applied.moves)
        conn.execute("UPDATE revisions SET state = 'discarded' WHERE id = ?", (prop["candidate_revision_id"],))
    for r in rows:
        decision = "excluded" if r["id"] in excluded else "accepted"
        conn.execute("UPDATE proposal_ops SET decision = ?, override_reason = ? WHERE id = ?",
                     (decision, overrides.get(r["id"]), r["id"]))
    conn.execute("UPDATE proposals SET status = 'approved', result_revision_id = ?, decided_at = ? "
                 "WHERE id = ?", (result, _now(), prop["id"]))
    _set_current(conn, pid, current, result)
    return result


def _auto_apply(conn: sqlite3.Connection, pid: str, prop_id: str) -> None:
    """Режим «правки Claude применяются сразу».

    Правки агента сразу становятся текущей версией, автор проверяет их после.
    Не применяются только правки, задевающие жёсткую защиту: её ставит автор,
    и обычно ровно затем, чтобы туда не лезли. Такие правки ждут его решения
    (review = 'held'). Если отложены все — предложение остаётся открытым.
    """
    with write(conn):
        prop = _one(conn, "SELECT * FROM proposals WHERE id = ?", prop_id)
        current = _one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        if prop["status"] != "open" or current != prop["base_revision_id"]:
            return
        rows = conn.execute("SELECT * FROM proposal_ops WHERE proposal_id = ? ORDER BY position",
                            (prop_id,)).fetchall()
        held = {r["id"] for r in rows if "protected" in _current_flags(conn, pid, r)}
        for op_id in held:
            conn.execute("UPDATE proposal_ops SET review = 'held' WHERE id = ?", (op_id,))
        if len(held) == len(rows):
            return
        _commit_decision(conn, pid, prop, rows, held, {}, "agent", current)
        conn.execute("UPDATE proposals SET auto_applied = 1 WHERE id = ?", (prop_id,))


def set_agent_mode(conn: sqlite3.Connection, pid: str, mode: str, actor: str) -> dict:
    """Применять ли правки Claude сразу. Решает только автор."""
    _only_user(actor, "менять порядок работы с правками Claude")
    if mode not in ("apply", "propose"):
        raise EdlError("bad_mode", "режим: apply (сразу) или propose (ждать утверждения)")
    with write(conn):
        _one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        conn.execute("UPDATE projects SET agent_mode = ? WHERE id = ?", (mode, pid))
    return get_project(conn, pid)


def set_loudness(conn: sqlite3.Connection, pid: str, lufs: float | None, actor: str) -> dict:
    """Цель громкости экспорта в LUFS или None — не выравнивать. Решает автор."""
    _only_user(actor, "менять громкость ролика")
    if lufs is not None and not -30 <= lufs <= -8:
        raise EdlError("bad_loudness", "громкость ролика — от −30 до −8 LUFS")
    with write(conn):
        _one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        conn.execute("UPDATE projects SET loudness_lufs = ? WHERE id = ?", (lufs, pid))
    return get_project(conn, pid)


def set_audio_source(conn: sqlite3.Connection, pid: str, source: str, actor: str) -> dict:
    """Чей звук идёт в ролик: screen — микрофон записи экрана, camera — звук
    сведённой камеры. Решает автор."""
    _only_user(actor, "выбирать звук ролика")
    if source not in ("screen", "camera"):
        raise EdlError("bad_audio_source", "звук ролика: screen или camera")
    if source == "camera":
        from longform import visual

        if not visual.cameras(conn, pid):
            raise EdlError("no_camera", "звук камеры можно взять, только когда камера сведена с записью экрана")
    with write(conn):
        _one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        conn.execute("UPDATE projects SET audio_source = ? WHERE id = ?", (source, pid))
    return get_project(conn, pid)


def audio_from(conn: sqlite3.Connection, pid: str) -> dict[str, tuple[str, int, float]]:
    """Звук камеры вместо звука записи экрана: {запись экрана: (камера, смещение, уплывание)}."""
    row = _one(conn, "SELECT audio_source FROM projects WHERE id = ?", pid)
    if row["audio_source"] != "camera":
        return {}
    from longform import visual

    return {main: (c.asset_id, c.offset_us, c.drift_ppm) for main, c in visual.cameras(conn, pid).items()}


def review_op(conn: sqlite3.Connection, pid: str, prop_id: str, op_id: str, *, action: str, actor: str,
              reason: str = "") -> dict:
    """Проверка правки после применения.

    keep   — оставить как есть;
    revert — вернуть: новая версия, где вырезанное снова на месте (или
             отменён возврат, или снята перестановка), граница — ровно там,
             где была правка;
    apply  — применить отложенную правку, которая задевает жёсткую защиту
             (нужна причина).
    """
    _only_user(actor, "проверять правки")
    prop = _one(conn, "SELECT * FROM proposals WHERE id = ? AND project_id = ?", prop_id, pid)
    op = _one(conn, "SELECT * FROM proposal_ops WHERE id = ? AND proposal_id = ?", op_id, prop_id)
    if action == "keep":
        if op["decision"] != "accepted":
            raise Conflict("not_applied", "эта правка не применена, оставлять нечего")
        with write(conn):
            conn.execute("UPDATE proposal_ops SET review = 'kept' WHERE id = ?", (op_id,))
        return get_proposal(conn, pid, prop_id)
    current = _one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
    if action == "revert":
        if op["decision"] != "accepted" or op["review"] == "reverted":
            raise Conflict("not_applied", "эта правка не применена или уже возвращена")
        why = f"автор вернул правку {op['position'] + 1} предложения {prop['number']}"
        if op["kind"] == "move":
            # Снять перестановку из слоя версии: кусок встаёт на место, как было
            undo = Op("unmove", op["asset_id"], op["src_start_us"], op["src_end_us"], MOVE_REASON, why,
                      undo_of=op_id)
        elif op["kind"] == "unmove":
            raise Conflict("not_revertible", "снятие перестановки не возвращается: переставьте кусок заново")
        else:
            undo = Op("restore" if op["kind"] == "remove" else "remove", op["asset_id"], op["src_start_us"],
                      op["src_end_us"], "restore" if op["kind"] == "remove" else "other", why)
        new_review = "reverted"
    elif action == "apply":
        if op["review"] != "held":
            raise Conflict("not_held", "эта правка не отложена")
        if not reason.strip():
            raise EdlError("no_reason", "правка задевает жёсткую защиту: нужна причина")
        undo = _op_from_row(op)
        new_review = "kept"
    else:
        raise EdlError("bad_action", "действие: keep, revert или apply")
    fix = create_proposal(conn, pid, base_revision_id=current, pass_="cleanup",
                          rationale=f"проверка автора: {action} правки {op['position'] + 1}",
                          policy_version="ручная", ops=[undo], actor=actor, align=False)
    approve(conn, pid, fix["id"], actor=actor, acknowledge=[o["id"] for o in fix["ops"]],
            overrides={o["id"]: reason.strip() or "решение автора при проверке" for o in fix["ops"]
                       if "protected" in o["risk_flags"]})
    with write(conn):
        conn.execute("UPDATE proposal_ops SET review = ? WHERE id = ?", (new_review, op_id))
    return get_proposal(conn, pid, prop_id)


def agent_review_counts(conn: sqlite3.Connection, pid: str, revision_id: str) -> dict:
    """Сколько применённых правок Claude автор ещё не проверил и сколько отложено."""
    chain, rid = [], revision_id
    while rid:
        chain.append(rid)
        rid = conn.execute("SELECT parent_id FROM revisions WHERE id = ?", (rid,)).fetchone()[0]
    marks = ",".join("?" * len(chain))
    row = conn.execute(
        f"SELECT SUM(o.decision = 'accepted' AND o.review = 'pending'), SUM(o.review = 'held') "
        f"FROM proposal_ops o JOIN proposals p ON p.id = o.proposal_id "
        f"WHERE p.auto_applied = 1 AND p.result_revision_id IN ({marks})", chain).fetchone()
    return {"unreviewed": row[0] or 0, "held": row[1] or 0}


def nudge_joint(conn: sqlite3.Connection, pid: str, *, revision_id: str, joint: int, side: str, frames: int,
                actor: str) -> dict:
    """«Вернуть начало/конец» на стыке: сдвиг границы на N кадров.

    side='before' — конец куска перед стыком, side='after' — начало куска
    после него. frames > 0 возвращает вырезанное, < 0 убирает ещё. Это
    правка автора: создаётся и сразу утверждается, граница ставится ровно
    туда, куда он сказал.
    """
    _only_user(actor, "сдвинуть стык")
    if side not in ("before", "after") or frames == 0 or abs(frames) > 250:
        raise EdlError("bad_nudge", "сдвиг: side before|after, frames от -250 до 250, не 0")
    edl = compile_revision(conn, pid, revision_id)
    js = joints(edl)
    if not 0 <= joint < len(js):
        raise EdlError("bad_joint", f"в версии нет стыка {joint}")
    j = js[joint]
    asset = j["before_asset"] if side == "before" else j["after_asset"]
    fps = edl.sources[asset].fps
    step = frame_to_us(abs(frames), fps)
    if side == "before":
        edge = j["before_src_end_us"]
        a, b = (edge, edge + step) if frames > 0 else (edge - step, edge)
    else:
        edge = j["after_src_start_us"]
        a, b = (edge - step, edge) if frames > 0 else (edge, edge + step)
    row = _one(conn, "SELECT clips_json, moves_json FROM revisions WHERE id = ? AND project_id = ?", revision_id, pid)
    layer = _moves(row)
    if frames > 0 and layer:
        # С перестановками «вернуть» можно, только если кадры встанут у этого
        # стыка: вырезанные — да, а переставленные или их место — в другом
        # месте ролика
        sources, order, _ = load_sources(conn, pid)
        mains = [x for x in order if x in {r[0] for r in conn.execute(
            "SELECT id FROM assets WHERE project_id = ? AND role = 'main'", (pid,))}]
        fa, fb = us_to_frame(max(0, a), fps), us_to_frame(b, fps)
        edge_f = us_to_frame(edge, fps)
        if not lands_next_to(kept_intervals(_clips(row), sources), layer, sources, mains, asset, fa, fb,
                             edge_f - 1 if side == "before" else None, edge_f if side == "after" else None):
            raise EdlError("move_joint", "эти кадры после возврата встанут в ролике не у этого стыка, а в другом "
                                         "месте (они в переставленном куске или на месте, откуда он взят). Чтобы "
                                         "расширить кусок, верните перестановку и переставьте заново")
    kind = "restore" if frames > 0 else "remove"
    op = Op(kind, asset, max(0, a), b, "restore" if kind == "restore" else "other",
            f"ручной сдвиг стыка {joint}: {'вернуть' if frames > 0 else 'убрать'} {abs(frames)} кадр(ов)")
    prop = create_proposal(conn, pid, base_revision_id=revision_id, pass_="cleanup",
                           rationale="ручная правка стыка", policy_version="ручная", ops=[op], actor=actor,
                           align=False)
    return approve(conn, pid, prop["id"], actor=actor, acknowledge=[o["id"] for o in prop["ops"]],
                   overrides={o["id"]: "ручная правка автора" for o in prop["ops"] if "protected" in o["risk_flags"]})


def reject(conn: sqlite3.Connection, pid: str, prop_id: str, actor: str) -> dict:
    """Отклонение: текущая версия не меняется, кандидат остаётся в истории."""
    _only_user(actor, "отклонить предложение")
    with write(conn):
        prop = _one(conn, "SELECT * FROM proposals WHERE id = ? AND project_id = ?", prop_id, pid)
        if prop["status"] != "open":
            raise Conflict("not_open", f"предложение уже в состоянии {prop['status']}")
        conn.execute("UPDATE proposals SET status = 'rejected', decided_at = ? WHERE id = ?", (_now(), prop_id))
        conn.execute("UPDATE proposal_ops SET decision = 'excluded' WHERE proposal_id = ?", (prop_id,))
        conn.execute("UPDATE revisions SET state = 'discarded' WHERE id = ?", (prop["candidate_revision_id"],))
    return get_proposal(conn, pid, prop_id)


# ---------- бриф ----------

def save_brief(conn: sqlite3.Connection, pid: str, data: dict, actor: str) -> dict:
    """Новая версия брифа. От автора — сразу действует, от агента — черновик."""
    if actor not in ACTORS:
        raise Forbidden("неизвестный автор")
    with write(conn):
        _one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM briefs WHERE project_id = ?", (pid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO briefs (id, project_id, version, data_json, created_by, created_at, confirmed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (_id("brf"), pid, version, json.dumps(data, ensure_ascii=False), actor, _now(),
             _now() if actor == "user" else None))
    return get_brief(conn, pid)


def confirm_brief(conn: sqlite3.Connection, pid: str, version: int, actor: str) -> dict:
    _only_user(actor, "подтвердить бриф")
    with write(conn):
        row = _one(conn, "SELECT * FROM briefs WHERE project_id = ? AND version = ?", pid, version)
        if row["confirmed_at"]:
            raise Conflict("already_confirmed", "эта версия брифа уже подтверждена")
        conn.execute("UPDATE briefs SET confirmed_at = ? WHERE id = ?", (_now(), row["id"]))
    return get_brief(conn, pid)


def get_brief(conn: sqlite3.Connection, pid: str) -> dict:
    """Действующий бриф (последний подтверждённый) и более новый черновик, если есть."""
    def as_dict(r):
        return None if r is None else {
            "version": r["version"], "data": json.loads(r["data_json"]), "created_by": r["created_by"],
            "created_at": r["created_at"], "confirmed_at": r["confirmed_at"]}
    confirmed = conn.execute("SELECT * FROM briefs WHERE project_id = ? AND confirmed_at IS NOT NULL "
                             "ORDER BY version DESC LIMIT 1", (pid,)).fetchone()
    draft = conn.execute("SELECT * FROM briefs WHERE project_id = ? AND confirmed_at IS NULL AND version > ? "
                         "ORDER BY version DESC LIMIT 1", (pid, confirmed["version"] if confirmed else 0)).fetchone()
    return {"active": as_dict(confirmed), "draft": as_dict(draft)}


# ---------- защищённые фрагменты ----------

def _check_range(conn, pid: str, asset_id: str, start_us: int, end_us: int) -> None:
    sources, _, _ = load_sources(conn, pid)
    src = sources.get(asset_id)
    if src is None:
        raise EdlError("unknown_asset", f"исходник {asset_id} не найден")
    if not 0 <= start_us < end_us <= frame_to_us(src.frame_count, src.fps):
        raise EdlError("out_of_bounds", "диапазон вне исходника")


def add_protection(conn: sqlite3.Connection, pid: str, *, asset_id: str, src_start_us: int, src_end_us: int,
                   kind: str, reason: str, actor: str) -> dict:
    if kind == "hard":
        _only_user(actor, "поставить жёсткую защиту")
    if kind not in ("hard", "soft"):
        raise EdlError("bad_kind", "защита бывает hard или soft")
    if not reason.strip():
        raise EdlError("no_reason", "у защиты должна быть причина")
    _check_range(conn, pid, asset_id, src_start_us, src_end_us)
    prt = _id("prt")
    with write(conn):
        conn.execute(
            "INSERT INTO protected_ranges (id, project_id, asset_id, src_start_us, src_end_us, kind, reason, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (prt, pid, asset_id, src_start_us, src_end_us, kind, reason.strip(), actor, _now()))
    return dict(_one(conn, "SELECT * FROM protected_ranges WHERE id = ?", prt))


def remove_protection(conn: sqlite3.Connection, pid: str, prt_id: str, actor: str) -> None:
    _only_user(actor, "снять защиту")
    with write(conn):
        _one(conn, "SELECT id FROM protected_ranges WHERE id = ? AND project_id = ? AND removed_at IS NULL",
             prt_id, pid)
        conn.execute("UPDATE protected_ranges SET removed_at = ? WHERE id = ?", (_now(), prt_id))


def list_protections(conn: sqlite3.Connection, pid: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM protected_ranges WHERE project_id = ? AND removed_at IS NULL "
                        "ORDER BY asset_id, src_start_us", (pid,))
    return [dict(r) for r in rows]


# ---------- комментарии ----------

def add_comment(
    conn: sqlite3.Connection, pid: str, *, text: str, actor: str, revision_id: str | None = None,
    edited_frame: int | None = None, asset_id: str | None = None, src_us: int | None = None,
    src_end_us: int | None = None,
) -> dict:
    """Комментарий к месту ролика. Хранится привязка ко времени исходника.

    Можно указать место в монтаже (кадр версии) — сервер сам переведёт его
    во время исходника — или сразу место в исходнике.
    """
    if actor not in ACTORS:
        raise Forbidden("неизвестный автор")
    if not text.strip():
        raise EdlError("empty_comment", "пустой комментарий")
    if revision_id is None:
        revision_id = _one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        if revision_id is None:
            raise EdlError("no_revision", "в проекте ещё нет версий")
    if edited_frame is not None:
        edl = compile_revision(conn, pid, revision_id)
        try:
            _, asset_id, src_frame = edl.edited_to_source(edited_frame)
        except IndexError:
            raise EdlError("out_of_bounds", f"кадра {edited_frame} нет в версии") from None
        # Якорь — середина кадра, а не его начало: начало кадра, округлённое
        # до микросекунды, при 29,97 бывает чуть раньше точного, и «кадр в
        # этот момент» оказался бы предыдущим
        fps = edl.sources[asset_id].fps
        src_us = (frame_to_us(src_frame, fps) + frame_to_us(src_frame + 1, fps)) // 2
        src_end_us = None
    elif asset_id is None or src_us is None:
        raise EdlError("no_anchor", "нужно место: кадр монтажа или место в исходнике")
    else:
        _check_range(conn, pid, asset_id, src_us, src_end_us if src_end_us is not None else src_us + 1)
    cid = _id("cmt")
    with write(conn):
        _one(conn, "SELECT id FROM revisions WHERE id = ? AND project_id = ?", revision_id, pid)
        conn.execute(
            "INSERT INTO comments (id, project_id, revision_id, asset_id, src_us, src_end_us, text, author, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, pid, revision_id, asset_id, src_us, src_end_us, text.strip(), actor, _now()))
    return dict(_one(conn, "SELECT * FROM comments WHERE id = ?", cid))


def list_comments(conn: sqlite3.Connection, pid: str, revision_id: str) -> list[dict]:
    """Комментарии с местом в выбранной версии.

    Если место вырезано в этой версии, edited_frame = None: комментарий не
    переносится на соседний кусок, а честно помечается «не входит в версию».
    """
    edl = compile_revision(conn, pid, revision_id)
    out = []
    for r in conn.execute("SELECT * FROM comments WHERE project_id = ? ORDER BY created_at", (pid,)):
        c = dict(r)
        fps = edl.sources[r["asset_id"]].fps
        c["edited_frame"] = edl.source_to_edited(r["asset_id"], frame_at(r["src_us"], fps))
        c["in_revision"] = c["edited_frame"] is not None
        out.append(c)
    return out


def resolve_comment(conn: sqlite3.Connection, pid: str, cid: str, actor: str) -> dict:
    _only_user(actor, "закрыть комментарий")
    with write(conn):
        _one(conn, "SELECT id FROM comments WHERE id = ? AND project_id = ?", cid, pid)
        conn.execute("UPDATE comments SET status = 'resolved' WHERE id = ?", (cid,))
    return dict(_one(conn, "SELECT * FROM comments WHERE id = ?", cid))


# ---------- метки ----------

def _anchor(conn: sqlite3.Connection, pid: str, revision_id: str | None, edited_frame: int | None,
            asset_id: str | None, src_us: int | None) -> tuple[str, int]:
    """Место -> (исходник, время исходника). Кадр монтажа переводится в
    середину соответствующего кадра исходника (см. add_comment)."""
    if edited_frame is not None:
        rid = revision_id or _one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        edl = compile_revision(conn, pid, rid)
        try:
            _, asset_id, src_frame = edl.edited_to_source(edited_frame)
        except IndexError:
            raise EdlError("out_of_bounds", f"кадра {edited_frame} нет в версии") from None
        fps = edl.sources[asset_id].fps
        return asset_id, (frame_to_us(src_frame, fps) + frame_to_us(src_frame + 1, fps)) // 2
    if asset_id is None or src_us is None:
        raise EdlError("no_anchor", "нужно место: кадр монтажа или место в исходнике")
    _check_range(conn, pid, asset_id, src_us, src_us + 1)
    return asset_id, src_us


def add_marker(conn: sqlite3.Connection, pid: str, *, actor: str, label: str = "", revision_id: str | None = None,
               edited_frame: int | None = None, asset_id: str | None = None, src_us: int | None = None,
               kind: str = "mark") -> dict:
    """Нумерованная метка — общий язык «между 3-й и 4-й меткой» (идея из kadr).
    Номера не переиспользуются: удалённая 3-я не превращает 4-ю в 3-ю.
    kind = 'chapter' — начало главы для YouTube, название обязательно."""
    if actor not in ACTORS:
        raise Forbidden("неизвестный автор")
    if kind not in ("mark", "chapter"):
        raise EdlError("bad_kind", "метка бывает обычной (mark) или началом главы (chapter)")
    if kind == "chapter" and not label.strip():
        raise EdlError("no_title", "у главы должно быть название")
    asset_id, src_us = _anchor(conn, pid, revision_id, edited_frame, asset_id, src_us)
    mid = _id("mrk")
    with write(conn):
        number = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM markers WHERE project_id = ?",
                              (pid,)).fetchone()[0]
        conn.execute("INSERT INTO markers (id, project_id, number, asset_id, src_us, label, created_by, created_at, "
                     "kind) VALUES (?,?,?,?,?,?,?,?,?)",
                     (mid, pid, number, asset_id, src_us, label.strip()[:200], actor, _now(), kind))
    return dict(_one(conn, "SELECT * FROM markers WHERE id = ?", mid))


def list_markers(conn: sqlite3.Connection, pid: str, revision_id: str | None = None) -> list[dict]:
    """Метки с местом в версии; вырезанное место — in_revision = False."""
    rid = revision_id or _one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
    edl = compile_revision(conn, pid, rid) if rid else None
    out = []
    for r in conn.execute("SELECT * FROM markers WHERE project_id = ? AND removed_at IS NULL ORDER BY number", (pid,)):
        m = dict(r)
        frame = None
        if edl is not None and r["asset_id"] in edl.sources:
            frame = edl.source_to_edited(r["asset_id"], frame_at(r["src_us"], edl.sources[r["asset_id"]].fps))
        m["edited_frame"], m["in_revision"] = frame, frame is not None
        out.append(m)
    return out


def remove_marker(conn: sqlite3.Connection, pid: str, marker_id: str, actor: str) -> None:
    _only_user(actor, "удалять метки")
    with write(conn):
        _one(conn, "SELECT id FROM markers WHERE id = ? AND project_id = ? AND removed_at IS NULL", marker_id, pid)
        conn.execute("UPDATE markers SET removed_at = ? WHERE id = ?", (_now(), marker_id))
