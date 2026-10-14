"""Отчёт качества экспорта.

Три статуса, которые не подменяют друг друга:
- technical — проверки кода по раскодированному результату;
- model_notes — замечания модели, каждое со ссылкой на диапазон;
- human — проверка человеком, ставит только автор.

Отчёт привязан к версии. Если текущая версия сменилась, отчёт помечается
устаревшим и к новой версии не относится.
"""

import json
import sqlite3

from longform.db import write
from longform.edl import CompiledEdl, EdlError
from longform.store import _id, _now, _one, _only_user
from longform.timebase import frame_to_us, us_to_frame


def _ancestry(conn: sqlite3.Connection, revision_id: str) -> list[str]:
    chain, rid = [], revision_id
    while rid:
        chain.append(rid)
        rid = conn.execute("SELECT parent_id FROM revisions WHERE id = ?", (rid,)).fetchone()[0]
    return chain


def unapproved_protected_cuts(conn: sqlite3.Connection, pid: str, revision_id: str,
                              edl: CompiledEdl) -> list[dict]:
    """Жёсткие защиты, вырезанные этой версией без записанной причины.

    Причина записывается при утверждении правки, задевающей защиту. Если
    защита вырезана, а такой правки с причиной в истории версии нет —
    это критическая ошибка.
    """
    chain = _ancestry(conn, revision_id)
    marks = ",".join("?" * len(chain))
    overrides = conn.execute(
        f"SELECT o.asset_id, o.src_start_us, o.src_end_us FROM proposal_ops o "
        f"JOIN proposals p ON p.id = o.proposal_id WHERE p.result_revision_id IN ({marks}) "
        f"AND o.decision = 'accepted' AND o.override_reason IS NOT NULL", chain).fetchall()
    problems = []
    for prot in conn.execute("SELECT * FROM protected_ranges WHERE project_id = ? AND kind = 'hard' "
                             "AND removed_at IS NULL", (pid,)):
        src = edl.sources.get(prot["asset_id"])
        if src is None:
            continue
        a, b = us_to_frame(prot["src_start_us"], src.fps), us_to_frame(prot["src_end_us"], src.fps)
        kept = sum(max(0, min(b, c.src_end_frame) - max(a, c.src_start_frame))
                   for c in edl.clips if c.asset_id == prot["asset_id"])
        if kept >= b - a:
            continue
        explained = any(o["asset_id"] == prot["asset_id"] and o["src_start_us"] < prot["src_end_us"]
                        and prot["src_start_us"] < o["src_end_us"] for o in overrides)
        if not explained:
            problems.append({"protection_id": prot["id"], "reason": prot["reason"],
                             "asset_id": prot["asset_id"], "src_start_us": prot["src_start_us"],
                             "src_end_us": prot["src_end_us"]})
    return problems


def create_report(conn: sqlite3.Connection, pid: str, revision_id: str, job_id: str, technical: dict) -> dict:
    critical = any(not check.get("ok", True) for check in technical.values() if isinstance(check, dict))
    qid = _id("qa")
    with write(conn):
        conn.execute(
            "INSERT INTO qa_reports (id, project_id, revision_id, job_id, technical_json, critical, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (qid, pid, revision_id, job_id, json.dumps(technical, ensure_ascii=False), int(critical), _now()))
    return get_report(conn, pid, qid)


def get_report(conn: sqlite3.Connection, pid: str, qid: str) -> dict:
    r = dict(_one(conn, "SELECT * FROM qa_reports WHERE id = ? AND project_id = ?", qid, pid))
    r["technical"] = json.loads(r.pop("technical_json"))
    r["model_notes"] = json.loads(r.pop("model_notes_json"))
    r["critical"] = bool(r["critical"])
    current = conn.execute("SELECT current_revision_id FROM projects WHERE id = ?", (pid,)).fetchone()[0]
    r["stale"] = r["revision_id"] != current
    # Финальным считается только экспорт без критических ошибок,
    # проверенный автором и относящийся к текущей версии
    r["final"] = not r["critical"] and r["human_status"] == "approved" and not r["stale"]
    return r


def list_reports(conn: sqlite3.Connection, pid: str) -> list[dict]:
    rows = conn.execute("SELECT id FROM qa_reports WHERE project_id = ? ORDER BY created_at DESC", (pid,))
    return [get_report(conn, pid, r["id"]) for r in rows.fetchall()]


def add_model_note(conn: sqlite3.Connection, pid: str, qid: str, *, text: str, out_start_frame: int,
                   out_end_frame: int, actor: str) -> dict:
    """Замечание модели обязано указывать диапазон ролика, без «уверенности в %»."""
    if not text.strip():
        raise EdlError("empty_note", "пустое замечание")
    with write(conn):
        row = _one(conn, "SELECT * FROM qa_reports WHERE id = ? AND project_id = ?", qid, pid)
        if not 0 <= out_start_frame < out_end_frame:
            raise EdlError("bad_range", "у замечания должен быть диапазон кадров")
        notes = json.loads(row["model_notes_json"])
        fps_row = _one(conn, "SELECT fps_num, fps_den FROM projects WHERE id = ?", pid)
        from fractions import Fraction

        fps = Fraction(fps_row["fps_num"], fps_row["fps_den"])
        notes.append({"text": text.strip(), "out_start_frame": out_start_frame, "out_end_frame": out_end_frame,
                      "out_start_us": frame_to_us(out_start_frame, fps), "author": actor, "at": _now()})
        conn.execute("UPDATE qa_reports SET model_notes_json = ? WHERE id = ?",
                     (json.dumps(notes, ensure_ascii=False), qid))
    return get_report(conn, pid, qid)


def set_human_review(conn: sqlite3.Connection, pid: str, qid: str, *, status: str, note: str, actor: str) -> dict:
    _only_user(actor, "отметить проверку человеком")
    if status not in ("approved", "changes_requested"):
        raise EdlError("bad_status", "проверка: approved или changes_requested")
    with write(conn):
        _one(conn, "SELECT id FROM qa_reports WHERE id = ? AND project_id = ?", qid, pid)
        conn.execute("UPDATE qa_reports SET human_status = ?, human_note = ?, human_at = ? WHERE id = ?",
                     (status, note.strip() or None, _now(), qid))
    return get_report(conn, pid, qid)
