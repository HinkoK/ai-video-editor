"""Миграции: старая база переходит на новую схему без потери данных.

Старая база заполняется прямыми записями, а не кодом приложения: код уже
знает о новых столбцах и на старой схеме не работает."""

import sqlite3
from importlib import resources

import pytest

from longform import store
from longform.db import connect, migrate
from longform.ops import Op
from longform.timebase import frame_to_us

pytestmark = pytest.mark.media
NOW = "2026-10-06T12:00:00+00:00"


def us(frame):
    return frame_to_us(frame, 25)


def migrate_to(conn: sqlite3.Connection, last: int) -> None:
    """Как db.migrate, но только до версии last включительно."""
    files = sorted((f for f in resources.files("longform.migrations").iterdir() if f.name.endswith(".sql")),
                   key=lambda f: f.name)
    for f in files:
        number = int(f.name.split("_", 1)[0])
        if number <= last:
            conn.executescript(f"BEGIN;\n{f.read_text()}\nPRAGMA user_version = {number};\nCOMMIT;")


def old_project(conn: sqlite3.Connection, media_path: str) -> dict:
    """Проект версии 4: файл, первая версия, предложение Claude из двух правок
    (одну автор оставил), готовый экспорт с отчётом качества."""
    ex = conn.execute
    ex("INSERT INTO projects (id, name, created_at, fps_num, fps_den, sample_rate, channels, width, height) "
       "VALUES ('prj_old', 'старая', ?, 25, 1, 48000, 1, 256, 144)", (NOW,))
    ex("INSERT INTO assets (id, project_id, position, path, size_bytes, mtime_ns, fingerprint, fps_num, fps_den, "
       "frame_count, sample_rate, channels, audio_offset_num, audio_offset_den, width, height, registered_at) "
       "VALUES ('ast_old', 'prj_old', 0, ?, 1, 1, 'f', 25, 1, 1500, 48000, 1, 0, 1, 256, 144, ?)", (media_path, NOW))
    clips = '[{"id": "c0", "asset_id": "ast_old", "src_start_us": 0, "src_end_us": 60000000}]'
    ex("INSERT INTO revisions VALUES ('rev_1', 'prj_old', 1, NULL, 'approved', ?, 'исходник', 'user', ?)", (clips, NOW))
    ex("INSERT INTO revisions VALUES ('rev_2', 'prj_old', 2, 'rev_1', 'approved', ?, 'чистка', 'agent', ?)", (clips, NOW))
    ex("UPDATE projects SET current_revision_id = 'rev_2' WHERE id = 'prj_old'")
    ex("INSERT INTO proposals (id, project_id, number, base_revision_id, candidate_revision_id, result_revision_id, "
       "pass, rationale, policy_version, status, created_by, created_at, auto_applied) "
       "VALUES ('prp_1', 'prj_old', 1, 'rev_1', 'rev_2', 'rev_2', 'cleanup', 'чистка', 'playbook-v2', 'approved', "
       "'agent', ?, 1)", (NOW,))
    for pos, (reason, review) in enumerate([("technical_dead_air", "kept"), ("other", "pending")]):
        ex("INSERT INTO proposal_ops (id, proposal_id, position, kind, asset_id, src_start_us, src_end_us, reason, "
           "why, decision, adjust_note, review) VALUES (?, 'prp_1', ?, 'remove', 'ast_old', ?, ?, ?, 'пауза', "
           "'accepted', 'граница в тишине', ?)", (f"op_{pos}", pos, us(100 + 200 * pos), us(200 + 200 * pos), reason,
                                                  review))
    ex("INSERT INTO jobs (id, project_id, type, priority, revision_id, params_json, params_hash, state, created_by, "
       "created_at) VALUES ('job_1', 'prj_old', 'export', 50, 'rev_2', '{}', 'h', 'completed', 'user', ?)", (NOW,))
    ex("INSERT INTO qa_reports (id, project_id, revision_id, job_id, technical_json, critical, created_at) "
       "VALUES ('qa_1', 'prj_old', 'rev_2', 'job_1', '{}', 0, ?)", (NOW,))
    return {"id": "prj_old", "asset": "ast_old"}


def snapshot(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id")]


OPS = ["id", "proposal_id", "position", "kind", "asset_id", "src_start_us", "src_end_us", "reason", "why",
       "decision", "adjust_note", "review"]
JOBS = ["id", "project_id", "type", "priority", "revision_id", "params_json", "params_hash", "state", "created_by"]


def test_old_database_upgrades_without_losing_edits_jobs_and_reports(tmp_path, fx_plain):
    conn = connect(tmp_path / "editor.db")
    migrate_to(conn, 4)
    p = old_project(conn, str(fx_plain.path))
    ops_before, jobs_before = snapshot(conn, "proposal_ops", OPS), snapshot(conn, "jobs", JOBS)
    # В старой схеме нет ни «ожидания», ни задачи синхронизации
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE proposal_ops SET reason = 'waiting' WHERE id = 'op_1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET type = 'sync' WHERE id = 'job_1'")

    assert migrate(conn) >= 8
    assert snapshot(conn, "proposal_ops", OPS) == ops_before  # те же правки, проверка автора, пометки
    assert snapshot(conn, "jobs", JOBS) == jobs_before
    assert conn.execute("SELECT job_id FROM qa_reports").fetchone()[0] == "job_1"  # связь отчёта с задачей цела
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # проверка связей снова включена
    for index in ("ops_by_proposal", "jobs_queue", "jobs_by_hash", "jobs_by_project"):
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (index,)).fetchone(), index
    # Старый файл стал записью экрана с микрофоном
    assert conn.execute("SELECT role FROM assets").fetchone()[0] == "main"

    # Новое работает поверх старых данных
    new = store.create_proposal(
        conn, p["id"], base_revision_id="rev_2", pass_="cleanup", rationale="ожидание", policy_version="t",
        ops=[Op("remove", p["asset"], us(800), us(1100), "waiting", "модель думает, на экране без изменений")],
        actor="agent", align=False)
    assert new["ops"][0]["reason"] == "waiting" and new["status"] == "approved"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE proposal_ops SET reason = 'faster' WHERE id = ?", (new["ops"][0]["id"],))
    conn.execute("UPDATE jobs SET type = 'sync' WHERE id = 'job_1'")  # теперь допустимо
    conn.close()


def test_moves_migration_keeps_old_edits_and_revisions_immutable(tmp_path, fx_plain):
    """017: старая база поднимается со всеми правками, у старых версий
    слой перестановок пустой, и его, как и куски, менять нельзя [R-005]."""
    conn = connect(tmp_path / "editor.db")
    migrate_to(conn, 16)
    p = old_project(conn, str(fx_plain.path))
    ops_before = snapshot(conn, "proposal_ops", OPS)
    with pytest.raises(sqlite3.IntegrityError):  # до 017 перестановки нет
        conn.execute("UPDATE proposal_ops SET kind = 'move' WHERE id = 'op_1'")

    assert migrate(conn) >= 17
    assert snapshot(conn, "proposal_ops", OPS) == ops_before
    assert [r[0] for r in conn.execute("SELECT moves_json FROM revisions ORDER BY number")] == ["[]", "[]"]
    for sql in ("UPDATE revisions SET moves_json = '[{}]' WHERE id = 'rev_2'",
                "UPDATE revisions SET clips_json = '[]' WHERE id = 'rev_2'"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    # Перестановка без места или снятие без своей перестановки база не примет
    for sql in ("UPDATE proposal_ops SET kind = 'move', reason = 'reorder' WHERE id = 'op_1'",
                "UPDATE proposal_ops SET kind = 'unmove', reason = 'reorder' WHERE id = 'op_1'",
                "UPDATE proposal_ops SET reason = 'reorder' WHERE id = 'op_1'"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # Новое работает поверх старых данных: перестановка в старом проекте
    new = store.create_proposal(
        conn, p["id"], base_revision_id="rev_2", pass_="cleanup", rationale="перестановка", policy_version="t",
        ops=[Op("move", p["asset"], us(100), us(200), "reorder", "автор попросил",
                target=(p["asset"], us(600), "after"))], actor="agent", align=False)
    assert new["status"] == "approved" and new["ops"][0]["kind"] == "move"
    rev = store.get_revision(conn, p["id"], new["result_revision_id"])
    assert rev["moves"][0]["op_id"] == new["ops"][0]["id"]
    conn.close()
