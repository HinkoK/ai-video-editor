"""Фоновые задачи: очередь, исполнитель, отмена, сбой, остановка, экспорт с отчётом."""

import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from longform import jobs, qa, store, worker
from longform.api import Settings, create_app
from longform.db import connect, migrate
from longform.edl import EdlError
from longform.ffmpeg import QUIET, run
from longform.ops import Op
from longform.probe import probe
from longform.store import Forbidden
from longform.timebase import frame_to_us
from longform.workspace import Workspace
from media_fixtures import read_frame_numbers

pytestmark = pytest.mark.media


def us(frame):
    return frame_to_us(frame, 25)


@pytest.fixture
def ws(tmp_path):
    w = Workspace(tmp_path / "рабочая папка")
    w.root.mkdir()
    c = connect(w.db)
    migrate(c)
    c.close()
    return w


@pytest.fixture
def conn(ws):
    c = connect(ws.db)
    yield c
    c.close()


@pytest.fixture
def pr(conn, fx_plain, fx_second):
    p = store.create_project(conn, "Урок: агенты", "user")
    cam = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    scr = store.register_asset(conn, p["id"], str(fx_second.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    store.set_agent_mode(conn, p["id"], "propose", "user")
    prop = store.create_proposal(
        conn, p["id"], base_revision_id=rev["id"], pass_="cleanup", rationale="чистка", policy_version="t",
        ops=[Op("remove", cam["id"], us(100), us(200), "technical_dead_air", "пауза"),
             Op("remove", scr["id"], us(10), us(50), "false_start", "фальстарт")], actor="agent")
    store.approve(conn, p["id"], prop["id"], actor="user")
    return SimpleNamespace(id=p["id"], cam=cam["id"], scr=scr["id"], first=rev["id"],
                           rev=store.get_project(conn, p["id"])["current_revision_id"])


def run_queue(ws):
    worker.serve(ws, stop={}, once=True)


def test_same_work_is_not_queued_twice(conn, ws, pr):
    a = jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    b = jobs.enqueue(conn, pr.id, "proxy", actor="agent", asset_id=pr.cam)
    assert a["id"] == b["id"]
    run_queue(ws)
    done = jobs.get_job(conn, pr.id, a["id"])
    assert done["state"] == "completed"
    # Готовый файл переиспользуется, пропавший — пересчитывается
    assert jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)["id"] == a["id"]
    shutil.move(done["output_path"], done["output_path"] + ".moved")
    assert jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)["id"] != a["id"]


def test_proxy_keeps_source_time(conn, ws, pr, fx_plain):
    job = jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    run_queue(ws)
    out = jobs.get_job(conn, pr.id, job["id"])["output_path"]
    # Кадр k лёгкой копии — кадр k исходника: время в браузере = время исходника
    assert read_frame_numbers(out) == list(range(fx_plain.frame_count))
    # Маленькое видео не растягивается вверх
    assert probe(out).video.height == 144


def test_preview_of_a_joint_is_exact(conn, ws, pr):
    job = jobs.enqueue(conn, pr.id, "preview", actor="agent", revision_id=pr.rev,
                       params={"out_start_frame": 80, "out_end_frame": 130})
    run_queue(ws)
    done = jobs.get_job(conn, pr.id, job["id"])
    assert done["state"] == "completed" and not done["draft"] and not done["stale"]
    # Вырезаны кадры 100–200: после 99-го сразу идёт 200-й
    assert read_frame_numbers(done["output_path"]) == list(range(80, 100)) + list(range(200, 230))


def test_export_with_quality_report(conn, ws, pr):
    job = jobs.enqueue(conn, pr.id, "export", actor="user", revision_id=pr.rev)
    run_queue(ws)
    done = jobs.get_job(conn, pr.id, job["id"])
    assert done["state"] == "completed", done["error"]
    assert "Урок_ агенты v2" in done["output_path"]
    (report,) = qa.list_reports(conn, pr.id)
    tech = report["technical"]
    assert tech["frames"]["ok"] and tech["audio_samples"]["ok"] and tech["matches_revision"]["ok"]
    assert not report["critical"]
    # Громкость выровнена до цели проекта (по умолчанию −16 LUFS) одним
    # усилением, и это перемерено по готовому ролику; сверка звука выше
    # прошла — усиление не сдвинуло и не обрезало звук
    loud = tech["loudness"]
    assert loud["target_lufs"] == -16.0 and loud["applied"]
    assert loud["after_lufs"] == pytest.approx(-16.0, abs=1.0) or loud["capped_by_peak"]
    assert loud["after_true_peak"] <= loud["true_peak_limit"] + 0.5
    # Субтитров нет (нет расшифровки), а ролик готов
    assert not Path(done["output_path"]).with_suffix(".srt").exists()
    # Пока автор не посмотрел, экспорт не финальный
    assert report["final"] is False
    qa.add_model_note(conn, pr.id, report["id"], text="на стыке 100 вдох обрезан", out_start_frame=95,
                      out_end_frame=105, actor="agent")
    with pytest.raises(Forbidden):
        qa.set_human_review(conn, pr.id, report["id"], status="approved", note="", actor="agent")
    report = qa.set_human_review(conn, pr.id, report["id"], status="approved", note="ок", actor="user")
    assert report["final"] is True and len(report["model_notes"]) == 1


def test_export_rules(conn, ws, pr):
    with pytest.raises(Forbidden):
        jobs.enqueue(conn, pr.id, "export", actor="agent", revision_id=pr.rev)
    prop = store.create_proposal(conn, pr.id, base_revision_id=pr.rev, pass_="cleanup", rationale="x",
                                 policy_version="t", ops=[Op("remove", pr.cam, us(300), us(400),
                                                             "technical_dead_air", "x")], actor="agent")
    with pytest.raises(EdlError):  # кандидат, не утверждён
        jobs.enqueue(conn, pr.id, "export", actor="user", revision_id=prop["candidate_revision_id"])


def test_export_refuses_unexplained_protected_cut(conn, ws, pr):
    # Защиту поставили уже после того, как этот кусок вырезали: причины в истории нет
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(150), src_end_us=us(160),
                         kind="hard", reason="оговорка", actor="user")
    job = jobs.enqueue(conn, pr.id, "export", actor="user", revision_id=pr.rev)
    run_queue(ws)
    done = jobs.get_job(conn, pr.id, job["id"])
    assert done["state"] == "failed" and "защищённое" in done["error"]


def test_stale_result_is_marked(conn, ws, pr):
    job = jobs.enqueue(conn, pr.id, "preview", actor="user", revision_id=pr.rev,
                       params={"out_start_frame": 0, "out_end_frame": 25})
    store.revert(conn, pr.id, pr.first, "user")
    assert jobs.get_job(conn, pr.id, job["id"])["stale"] is True


def slow_runner(marker: str):
    """Настоящий долгий ffmpeg, который проверяет отмену как обычная задача."""
    def runner(ctx):
        # -re: читать вход в реальном времени, то есть ffmpeg будет работать минуты
        run(["ffmpeg", *QUIET, "-re", "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={marker}",
             "-f", "null", "-"], cancel=ctx.cancel)
        raise AssertionError("не должен дойти сюда")
    return runner


def ffmpeg_alive(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", f"d={marker}"], capture_output=True).returncode == 0


def wait_for(cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_cancel_kills_ffmpeg(conn, ws, pr, monkeypatch):
    marker = "987.123"
    monkeypatch.setitem(worker.RUNNERS, "proxy", slow_runner(marker))
    job = jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    t = threading.Thread(target=run_queue, args=(ws,))
    t.start()
    assert wait_for(lambda: ffmpeg_alive(marker))
    jobs.request_cancel(conn, pr.id, job["id"], "user")
    t.join(timeout=10)
    assert jobs.get_job(conn, pr.id, job["id"])["state"] == "cancelled"
    assert not ffmpeg_alive(marker), "ffmpeg остался сиротой"


def test_shutdown_interrupts_and_next_start_resumes(conn, ws, pr, monkeypatch):
    marker = "876.123"
    monkeypatch.setitem(worker.RUNNERS, "proxy", slow_runner(marker))
    job = jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    stop: dict = {}
    t = threading.Thread(target=worker.serve, args=(ws, stop))
    t.start()
    assert wait_for(lambda: ffmpeg_alive(marker))
    stop["reason"] = "shutdown"  # так делает обработчик SIGTERM
    t.join(timeout=10)
    assert jobs.get_job(conn, pr.id, job["id"])["state"] == "interrupted"
    assert not ffmpeg_alive(marker)
    monkeypatch.undo()
    run_queue(ws)  # следующий запуск исполнителя
    done = jobs.get_job(conn, pr.id, job["id"])
    assert done["state"] == "completed" and done["attempts"] == 2


def test_crash_recovery_has_a_limit(conn, ws, pr):
    job = jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    # Как будто исполнитель упал посреди задачи уже в третий раз
    conn.execute("UPDATE jobs SET state = 'running', attempts = 3 WHERE id = ?", (job["id"],))
    run_queue(ws)
    done = jobs.get_job(conn, pr.id, job["id"])
    assert done["state"] == "failed" and "слишком много" in done["error"]


def test_only_one_worker(ws):
    first = worker.acquire_lock(ws)
    assert first is not None
    assert worker.acquire_lock(ws) is None
    first.close()


def test_changed_source_is_refused(conn, ws, fx_plain, tmp_path):
    copy = tmp_path / "копия.mp4"
    shutil.copy(fx_plain.path, copy)
    p = store.create_project(conn, "подмена", "user")
    a = store.register_asset(conn, p["id"], str(copy), "user")
    with copy.open("ab") as f:
        f.write(b"\0" * 4096)
    job = jobs.enqueue(conn, p["id"], "proxy", actor="user", asset_id=a["id"])
    run_queue(ws)
    done = jobs.get_job(conn, p["id"], job["id"])
    assert done["state"] == "failed" and "изменился" in done["error"]


def test_media_is_served_with_ranges_and_cookie(conn, ws, pr):
    job = jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    run_queue(ws)
    client = TestClient(create_app(Settings(
        db_path=ws.db, user_token="u", agent_token="a", allowed_hosts=frozenset({"testserver"}),
        allowed_origins=frozenset({"http://testserver"}), workspace_root=ws.root)))
    url = f"/api/projects/{pr.id}/assets/{pr.cam}/proxy"
    assert client.get(url).status_code == 401
    # Ключ Claude нельзя превратить в cookie браузера
    assert client.post("/api/session/cookie", headers={"Authorization": "Bearer a"}).status_code == 403
    assert client.post("/api/session/cookie", headers={"Authorization": "Bearer u"}).status_code == 204
    part = client.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert client.get(f"/api/projects/{pr.id}/jobs/{job['id']}/output").status_code == 200


def test_export_when_a_whole_file_is_dropped(conn, ws, pr):
    """Версия целиком убирает второй файл (экран): экспорт и сверка — только
    по использованным файлам. Найдено независимой проверкой: падало KeyError."""
    prop = store.create_proposal(conn, pr.id, base_revision_id=pr.rev, pass_="cleanup", rationale="без экрана",
                                 policy_version="t", actor="agent",
                                 ops=[Op("remove", pr.scr, 0, us(50), "recording_setup", "x"),
                                      Op("remove", pr.scr, us(50), us(500), "other", "весь экран не нужен")])
    store.approve(conn, pr.id, prop["id"], actor="user", acknowledge=[o["id"] for o in prop["ops"]])
    rid = store.get_project(conn, pr.id)["current_revision_id"]
    job = jobs.enqueue(conn, pr.id, "export", actor="user", revision_id=rid)
    run_queue(ws)
    done = jobs.get_job(conn, pr.id, job["id"])
    assert done["state"] == "completed", done["error"]
    assert not qa.list_reports(conn, pr.id)[0]["critical"]


def test_unverified_export_never_gets_a_final_name(conn, ws, pr, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("сверка сломалась")
    monkeypatch.setattr(worker, "verify_output", broken)
    job = jobs.enqueue(conn, pr.id, "export", actor="user", revision_id=pr.rev)
    run_queue(ws)
    assert jobs.get_job(conn, pr.id, job["id"])["state"] == "failed"
    assert list((ws.root / "projects" / pr.id / "exports").glob("*.mp4")) == []


def test_worker_heartbeat_continues_during_a_long_job(conn, ws, pr, monkeypatch):
    """Во время долгой задачи исполнитель должен сигналить, что жив: иначе
    интерфейс советует перезапуск, и это прерывает экспорт."""
    marker = "765.123"
    monkeypatch.setitem(worker.RUNNERS, "proxy", slow_runner(marker))
    job = jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    t = threading.Thread(target=run_queue, args=(ws,))
    t.start()
    assert wait_for(lambda: ffmpeg_alive(marker))
    time.sleep(4.5)
    status = jobs.worker_status(conn)
    jobs.request_cancel(conn, pr.id, job["id"], "user")
    t.join(timeout=10)
    assert status["alive"] and status["heartbeat_age_s"] < 4


def test_orphaned_ffmpeg_is_killed_but_a_stranger_is_not(ws):
    """Жёстко убитый исполнитель оставляет ffmpeg в своей группе процессов.
    Следующий запуск добивает его по записи — но только если это наш
    инструмент: номер процесса мог достаться другой программе."""
    import json

    orphan = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                               "-i", "anullsrc=d=654.321", "-f", "null", "-"], start_new_session=True)
    (ws.run_dir / worker.CHILD_FILE).write_text(json.dumps({"pid": orphan.pid}))
    assert worker.kill_orphan(ws) == orphan.pid
    assert orphan.wait(timeout=5) != 0

    stranger = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (ws.run_dir / worker.CHILD_FILE).write_text(json.dumps({"pid": stranger.pid}))
    assert worker.kill_orphan(ws) is None
    assert stranger.poll() is None
    stranger.kill()


def test_preview_near_the_end_is_clamped(conn, ws, pr):
    client = TestClient(create_app(Settings(
        db_path=ws.db, user_token="u", agent_token="a", allowed_hosts=frozenset({"testserver"}),
        allowed_origins=frozenset(), workspace_root=ws.root)))
    total = store.get_revision(conn, pr.id, pr.rev)["total_frames"]
    r = client.post(f"/api/projects/{pr.id}/revisions/{pr.rev}/preview", headers={"Authorization": "Bearer a"},
                    json={"out_start_frame": total - 40, "out_end_frame": total + 75})
    assert r.status_code == 201 and r.json()["params"]["out_end_frame"] == total
    # Агент не выбирает язык расшифровки: повторная расшифровка снимает мягкую защиту
    r = client.post(f"/api/projects/{pr.id}/assets/{pr.cam}/transcribe", headers={"Authorization": "Bearer a"},
                    json={"language": "uk"})
    assert r.json()["params"]["language"] == "ru"


def test_contact_sheet_shows_exact_frames(conn, ws, pr):
    """«Глаза» Claude: лист кадров в заданные моменты. Номера кадров вшиты в
    синтетику, по ним видно, что показан именно нужный кадр."""
    import numpy as np

    from media_fixtures import H, W, read_frame_number

    client = TestClient(create_app(Settings(
        db_path=ws.db, user_token="u", agent_token="a", allowed_hosts=frozenset({"testserver"}),
        allowed_origins=frozenset(), workspace_root=ws.root)))
    mid = lambda k: (frame_to_us(k, 25) + frame_to_us(k + 1, 25)) // 2  # noqa: E731
    points = [{"asset_id": pr.cam, "src_us": mid(k)} for k in (37, 500, 1499)]
    points.append({"edited_frame": 150})  # в версии вырезаны кадры 100–200: это кадр 250 исходника
    r = client.post(f"/api/projects/{pr.id}/frames", headers={"Authorization": "Bearer a"},
                    json={"points": points, "width": W, "cols": 2})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    png = tmp = ws.root / "лист.png"
    tmp.write_bytes(r.content)
    raw = run(["ffmpeg", "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    sheet = np.frombuffer(raw, np.uint8).reshape(2 * H + 4, 2 * W + 4)
    tiles = [sheet[r0:r0 + H, c0:c0 + W] for r0 in (0, H + 4) for c0 in (0, W + 4)]
    assert [read_frame_number(t) for t in tiles] == [37, 500, 1499, 250]
    # Произвольный путь задать нельзя: только подключённые файлы проекта
    bad = client.post(f"/api/projects/{pr.id}/frames", headers={"Authorization": "Bearer a"},
                      json={"points": [{"asset_id": "../../etc/passwd", "src_us": 0}]})
    assert bad.status_code == 404


def test_storage_usage_and_cleaning(conn, ws, pr):
    from longform import storage

    jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    jobs.enqueue(conn, pr.id, "export", actor="user", revision_id=pr.rev)
    run_queue(ws)
    use = {c["key"]: c for c in storage.usage(conn, ws, pr.id)["categories"]}
    assert use["proxies"]["bytes"] > 0 and use["exports"]["bytes"] > 0 and use["cache"]["bytes"] > 0
    with pytest.raises(Forbidden):
        storage.clean(conn, ws, pr.id, "proxies", "agent")
    with pytest.raises(EdlError):  # готовые ролики не стираются
        storage.clean(conn, ws, pr.id, "exports", "user")
    after = {c["key"]: c for c in storage.clean(conn, ws, pr.id, "proxies", "user")["categories"]}
    assert after["proxies"]["bytes"] == 0 and after["exports"]["bytes"] == use["exports"]["bytes"]
    # Стёртая копия пересобирается по новому запросу
    again = jobs.enqueue(conn, pr.id, "proxy", actor="user", asset_id=pr.cam)
    assert again["state"] == "queued"
    with pytest.raises(store.Conflict):  # пока что-то считается, чистить нельзя
        storage.clean(conn, ws, pr.id, "cache", "user")


def test_export_of_a_moved_piece_is_verified_at_its_joints(conn, ws, pr):
    """Перестановка (step-29): экспорт версии с переставленным куском — кадры
    по номерам как в версии, сверка проходит, три стыка перестановки в ней."""
    prop = store.create_proposal(
        conn, pr.id, base_revision_id=pr.rev, pass_="cleanup", rationale="перестановка", policy_version="t",
        ops=[Op("move", pr.cam, us(1000), us(1200), "reorder", "автор попросил", target=(pr.cam, us(300), "after"))],
        actor="user")
    done = store.approve(conn, pr.id, prop["id"], actor="user", acknowledge=[o["id"] for o in prop["ops"]])
    rid = done["result_revision_id"]
    job = jobs.enqueue(conn, pr.id, "export", actor="user", revision_id=rid)
    run_queue(ws)
    out = jobs.get_job(conn, pr.id, job["id"])
    assert out["state"] == "completed", out["error"]
    edl = store.compile_revision(conn, pr.id, rid)
    numbers = {pr.cam: 0, pr.scr: 30000}
    assert read_frame_numbers(out["output_path"]) == [
        numbers[c.asset_id] + f for c in edl.clips for f in range(c.src_start_frame, c.src_end_frame)]
    report = next(r for r in qa.list_reports(conn, pr.id) if r["revision_id"] == rid)
    check = report["technical"]["matches_revision"]
    assert check["ok"] and check["move_joints"] == 3
    assert report["technical"]["frames"]["ok"] and report["technical"]["audio_samples"]["ok"]
