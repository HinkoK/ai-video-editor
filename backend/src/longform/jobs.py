"""Очередь фоновых задач в SQLite.

Сервер только ставит задачи и читает их состояние. Выполняет их отдельный
процесс-исполнитель (worker.py), по одной тяжёлой за раз. Задача привязана
к версии монтажа и к отпечатку всего, от чего зависит результат: если
такой же результат уже есть или уже считается, новая задача не ставится.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from longform.db import write
from longform.edl import EdlError
from longform.store import Forbidden, NotFound, _id, _now, _one, _only_user

PRIORITY = {"preview": 10, "proxy": 20, "sync": 25, "analyze": 30, "transcribe": 40, "noises": 45, "export": 50}
# Камера: только лёгкая копия и синхронизация; её звук не расшифровывается
CAMERA_JOBS = ("proxy", "sync")
MAX_ATTEMPTS = 3
# Меняется при изменении склейки или расшифровки: старые результаты
# перестают совпадать по отпечатку и пересчитываются
RENDERER_VERSION = "select-6-side"
ACTIVE = ("queued", "running")


def _content_key(conn: sqlite3.Connection, pid: str, asset_id: str | None, revision_id: str | None) -> dict:
    key: dict = {"renderer": RENDERER_VERSION}
    if revision_id:
        rev = _one(conn, "SELECT clips_json FROM revisions WHERE id = ? AND project_id = ?", revision_id, pid)
        key["clips"] = json.loads(rev["clips_json"])
        assets = sorted({c["asset_id"] for c in key["clips"]})
    else:
        assets = [asset_id] if asset_id else []
    key["assets"] = {
        a: _one(conn, "SELECT fingerprint FROM assets WHERE id = ? AND project_id = ?", a, pid)["fingerprint"]
        for a in assets
    }
    if revision_id:
        # Картинка ролика зависит и от слоя кадра: поменяли раскладку или
        # синхронизацию камеры — пробный ролик и экспорт собираются заново
        from longform import graphics, store, visual

        key["visual"] = visual.fingerprint(conn, pid)
        key["graphics"] = graphics.fingerprint(conn, pid)
        key["audio_from"] = store.audio_from(conn, pid)
    return key


def enqueue(
    conn: sqlite3.Connection, pid: str, job_type: str, *, actor: str,
    asset_id: str | None = None, revision_id: str | None = None, params: dict | None = None,
) -> dict:
    if job_type not in PRIORITY:
        raise EdlError("bad_job", f"неизвестный тип задачи {job_type!r}")
    if job_type == "export":
        _only_user(actor, "запустить экспорт")
    if asset_id is not None:
        role = conn.execute("SELECT role FROM assets WHERE id = ? AND project_id = ?", (asset_id, pid)).fetchone()
        if role is not None and role[0] == "camera" and job_type not in CAMERA_JOBS:
            raise EdlError("camera_job", "у камеры звук только для синхронизации: расшифровка и паузы — "
                                         "по записи экрана с микрофоном")
        if job_type == "sync" and (role is None or role[0] != "camera"):
            raise EdlError("not_camera", "синхронизируется камера, а не запись экрана")
    if actor not in ("user", "agent"):
        raise Forbidden("неизвестный автор")
    params = params or {}
    if job_type == "export":
        rev = _one(conn, "SELECT state FROM revisions WHERE id = ? AND project_id = ?", revision_id, pid)
        if rev["state"] != "approved":
            raise EdlError("not_approved", "финальный экспорт — только из утверждённой версии")
    key = {"type": job_type, "params": params, **_content_key(conn, pid, asset_id, revision_id)}
    params_hash = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:32]
    with write(conn):
        same = conn.execute(
            "SELECT * FROM jobs WHERE project_id = ? AND params_hash = ? AND state IN "
            "('queued','running','completed') ORDER BY created_at DESC", (pid, params_hash)).fetchall()
        for row in same:
            # Готовый результат переиспользуем, только если файл на месте
            if row["state"] in ACTIVE or (row["output_path"] and Path(row["output_path"]).exists()):
                return get_job(conn, pid, row["id"])
        jid = _id("job")
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, priority, asset_id, revision_id, params_json, params_hash, "
            "state, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,'queued',?,?)",
            (jid, pid, job_type, PRIORITY[job_type], asset_id, revision_id, json.dumps(params), params_hash,
             actor, _now()))
    return get_job(conn, pid, jid)


def _job_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json"))
    if row["revision_id"]:
        rev = conn.execute("SELECT state, number FROM revisions WHERE id = ?", (row["revision_id"],)).fetchone()
        current = conn.execute("SELECT current_revision_id FROM projects WHERE id = ?",
                               (row["project_id"],)).fetchone()[0]
        d["revision_number"] = rev["number"]
        # Черновик: собран из версии-кандидата, которую ещё не утвердили
        d["draft"] = rev["state"] == "candidate"
        # Устарел: версия больше не текущая. Такой результат не выдаётся за текущий
        d["stale"] = rev["state"] == "approved" and row["revision_id"] != current
    return d


def get_job(conn: sqlite3.Connection, pid: str, jid: str) -> dict:
    return _job_dict(conn, _one(conn, "SELECT * FROM jobs WHERE id = ? AND project_id = ?", jid, pid))


def list_jobs(conn: sqlite3.Connection, pid: str, limit: int = 100) -> list[dict]:
    rows = conn.execute("SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?", (pid, limit))
    return [_job_dict(conn, r) for r in rows.fetchall()]


def request_cancel(conn: sqlite3.Connection, pid: str, jid: str, actor: str) -> dict:
    _only_user(actor, "отменить задачу")
    with write(conn):
        row = _one(conn, "SELECT * FROM jobs WHERE id = ? AND project_id = ?", jid, pid)
        if row["state"] == "queued":
            conn.execute("UPDATE jobs SET state = 'cancelled', finished_at = ?, message = 'отменена до начала' "
                         "WHERE id = ?", (_now(), jid))
        elif row["state"] == "running":
            conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (jid,))
    return get_job(conn, pid, jid)


# ---------- для исполнителя ----------

def recover(conn: sqlite3.Connection) -> list[str]:
    """При запуске исполнителя: всё, что «шло», осталось от упавшего или
    остановленного процесса. Возвращаем в очередь, пока не кончились попытки.

    Второй живой исполнитель невозможен: его не пускает замок (worker.lock).
    """
    with write(conn):
        conn.execute("UPDATE jobs SET state = 'interrupted', message = 'исполнитель остановился посреди задачи' "
                     "WHERE state = 'running'")
        rows = conn.execute("SELECT id, attempts FROM jobs WHERE state = 'interrupted'").fetchall()
        requeued = []
        for r in rows:
            if r["attempts"] >= MAX_ATTEMPTS:
                conn.execute("UPDATE jobs SET state = 'failed', finished_at = ?, "
                             "error = 'задача прерывалась слишком много раз' WHERE id = ?", (_now(), r["id"]))
            else:
                conn.execute("UPDATE jobs SET state = 'queued', cancel_requested = 0, progress = 0, "
                             "message = 'прервана, запущена снова' WHERE id = ?", (r["id"],))
                requeued.append(r["id"])
    return requeued


def claim(conn: sqlite3.Connection, worker_pid: int) -> sqlite3.Row | None:
    with write(conn):
        row = conn.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY priority, created_at LIMIT 1"
                           ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET state = 'running', attempts = attempts + 1, started_at = ?, heartbeat_at = ?, "
                     "worker_pid = ?, progress = 0 WHERE id = ?", (_now(), _now(), worker_pid, row["id"]))
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()


def progress(conn: sqlite3.Connection, jid: str, value: float, message: str | None = None) -> None:
    with write(conn):
        if message is None:
            conn.execute("UPDATE jobs SET progress = ?, heartbeat_at = ? WHERE id = ?", (value, _now(), jid))
        else:
            conn.execute("UPDATE jobs SET progress = ?, message = ?, heartbeat_at = ? WHERE id = ?",
                         (value, message, _now(), jid))


def cancel_requested(conn: sqlite3.Connection, jid: str) -> bool:
    row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (jid,)).fetchone()
    return bool(row and row[0])


def finish(conn: sqlite3.Connection, jid: str, state: str, *, output_path: Path | None = None,
           error: str | None = None, message: str = "") -> None:
    with write(conn):
        conn.execute(
            "UPDATE jobs SET state = ?, output_path = ?, error = ?, message = ?, finished_at = ?, "
            "progress = CASE WHEN ? = 'completed' THEN 1 ELSE progress END WHERE id = ?",
            (state, str(output_path) if output_path else None, error, message, _now(), state, jid))


def heartbeat(conn: sqlite3.Connection, pid: int, started_at: str) -> None:
    with write(conn):
        conn.execute("INSERT INTO worker_status (id, pid, started_at, heartbeat_at) VALUES (1, ?, ?, ?) "
                     "ON CONFLICT (id) DO UPDATE SET pid = excluded.pid, started_at = excluded.started_at, "
                     "heartbeat_at = excluded.heartbeat_at", (pid, started_at, _now()))


def worker_status(conn: sqlite3.Connection) -> dict:
    from datetime import UTC, datetime

    row = conn.execute("SELECT * FROM worker_status WHERE id = 1").fetchone()
    if row is None:
        return {"alive": False, "pid": None, "heartbeat_age_s": None}
    age = (datetime.now(UTC) - datetime.fromisoformat(row["heartbeat_at"])).total_seconds()
    return {"alive": age < 10, "pid": row["pid"], "heartbeat_age_s": round(age, 1)}


def output_of(conn: sqlite3.Connection, pid: str, jid: str) -> Path:
    row = _one(conn, "SELECT * FROM jobs WHERE id = ? AND project_id = ?", jid, pid)
    if row["state"] != "completed" or not row["output_path"]:
        raise NotFound("результата нет")
    return Path(row["output_path"])
