"""Сервер: граница доверия и полный путь правки через HTTP."""

import pytest
from fastapi.testclient import TestClient

from longform.api import Settings, create_app
from longform.db import connect, migrate
from longform.timebase import frame_to_us

pytestmark = pytest.mark.media

USER = {"Authorization": "Bearer user-token"}
AGENT = {"Authorization": "Bearer agent-token"}


def us(frame):
    return frame_to_us(frame, 25)


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "editor.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    app = create_app(Settings(
        db_path=db, user_token="user-token", agent_token="agent-token",
        allowed_hosts=frozenset({"testserver"}), allowed_origins=frozenset({"http://testserver"}),
    ))
    return TestClient(app)


@pytest.fixture
def project(client, fx_plain, fx_second):
    pid = client.post("/api/projects", json={"name": "Урок"}, headers=USER).json()["id"]
    cam = client.post(f"/api/projects/{pid}/assets", json={"path": str(fx_plain.path)}, headers=USER).json()
    scr = client.post(f"/api/projects/{pid}/assets", json={"path": str(fx_second.path)}, headers=USER).json()
    rev = client.post(f"/api/projects/{pid}/revisions/initial", headers=USER).json()
    assert client.post(f"/api/projects/{pid}/settings", headers=AGENT, json={"agent_mode": "propose"}).status_code == 403
    client.post(f"/api/projects/{pid}/settings", headers=USER, json={"agent_mode": "propose"})
    return {"id": pid, "cam": cam["id"], "scr": scr["id"], "rev": rev["id"]}


def remove(asset, a, b, reason="technical_dead_air"):
    return {"kind": "remove", "asset_id": asset, "src_start_us": us(a), "src_end_us": us(b),
            "reason": reason, "why": "пауза"}


def test_health_needs_no_key(client):
    assert client.get("/api/health").json() == {"ok": True}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "user-token"}])
def test_key_is_required(client, headers):
    r = client.get("/api/projects", headers=headers)
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


def test_foreign_host_is_rejected(client):
    # Подмена DNS: чужое имя указывает на 127.0.0.1, браузер шлёт Host этого имени
    r = client.get("/api/projects", headers={**USER, "Host": "evil.example"})
    assert r.status_code == 400


def test_foreign_origin_is_rejected_even_with_key(client):
    r = client.get("/api/projects", headers={**USER, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_session_tells_who_is_asking(client):
    assert client.get("/api/session", headers=USER).json() == {"actor": "user"}
    assert client.get("/api/session", headers=AGENT).json() == {"actor": "agent"}


def test_agent_scope(client, project, fx_plain):
    pid = project["id"]
    prop = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": project["rev"], "rationale": "чистка", "ops": [remove(project["cam"], 100, 200)]})
    assert prop.status_code == 201
    prop_id = prop.json()["id"]
    forbidden = [
        ("post", "/api/projects", {"name": "x"}),
        ("post", f"/api/projects/{pid}/assets", {"path": str(fx_plain.path)}),
        ("post", f"/api/projects/{pid}/proposals/{prop_id}/approve", {}),
        ("post", f"/api/projects/{pid}/proposals/{prop_id}/reject", None),
        ("post", f"/api/projects/{pid}/revisions/{project['rev']}/revert", None),
        ("post", f"/api/projects/{pid}/protected", {"asset_id": project["cam"], "src_start_us": 0,
                                                    "src_end_us": us(5), "kind": "hard", "reason": "x"}),
        ("post", f"/api/projects/{pid}/brief/1/confirm", None),
    ]
    for method, url, body in forbidden:
        r = getattr(client, method)(url, headers=AGENT, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, url
    # А это агенту можно
    allowed = [
        (f"/api/projects/{pid}/protected", {"asset_id": project["cam"], "src_start_us": 0,
                                            "src_end_us": us(5), "kind": "soft", "reason": "число"}),
        (f"/api/projects/{pid}/brief", {"audience": "новички"}),
        (f"/api/projects/{pid}/comments", {"text": "тут спорный стык", "edited_frame": 100}),
    ]
    for url, body in allowed:
        assert client.post(url, headers=AGENT, json=body).status_code == 201, url
    assert client.get(f"/api/projects/{pid}/brief", headers=AGENT).json()["draft"]["created_by"] == "agent"


def test_full_flow_with_partial_approval(client, project):
    pid = project["id"]
    prop = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": project["rev"], "rationale": "чистка", "policy_version": "playbook-test",
        "ops": [remove(project["cam"], 100, 200), remove(project["scr"], 10, 50, reason="false_start")],
    }).json()
    keep_scr = prop["ops"][1]["id"]
    done = client.post(f"/api/projects/{pid}/proposals/{prop['id']}/approve", headers=USER,
                       json={"exclude_op_ids": [keep_scr]}).json()
    assert done["status"] == "approved"
    rev = client.get(f"/api/projects/{pid}/revisions/{done['result_revision_id']}", headers=AGENT).json()
    assert rev["total_frames"] == 2000 - 100
    assert client.get(f"/api/projects/{pid}", headers=USER).json()["current_revision_id"] == rev["id"]


def test_errors_are_readable(client, project):
    pid = project["id"]
    r = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": project["rev"], "rationale": "x", "ops": [remove(project["cam"], 100, 200, "boring")]})
    assert r.status_code == 422 and r.json()["code"] == "bad_reason"
    # Лишнее поле в запросе — ошибка, а не молчаливый пропуск
    r = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": project["rev"], "rationale": "x", "ops": [], "approve": True})
    assert r.status_code == 422
    first = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": project["rev"], "rationale": "x", "ops": [remove(project["cam"], 100, 200)]}).json()
    client.post(f"/api/projects/{pid}/proposals/{first['id']}/approve", headers=USER, json={})
    stale = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": project["rev"], "rationale": "x", "ops": [remove(project["cam"], 300, 400)]})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_base"
    assert client.get(f"/api/projects/{pid}/proposals/nope", headers=USER).status_code == 404


def test_unreadable_file_gives_readable_error(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "битый"}, headers=USER).json()["id"]
    bad = tmp_path / "битый.mp4"
    bad.write_bytes(b"not a video at all" * 100)
    r = client.post(f"/api/projects/{pid}/assets", json={"path": str(bad)}, headers=USER)
    assert r.status_code == 422
    assert r.json()["code"] in ("unreadable_media", "unsupported_media")


def test_parallel_requests_from_a_browser(tmp_path, fx_plain):
    """Браузер шлёт запросы параллельно. FastAPI открывает соединение с базой
    в одном рабочем потоке, а обработчик выполняет в другом — SQLite это по
    умолчанию запрещает. Найдено живым браузером: тестовый клиент ходит
    последовательно и ошибку не видит."""
    import json as _json
    import socket
    import threading
    import time
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    import uvicorn

    db = tmp_path / "editor.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Settings(db_path=db, user_token="u", agent_token="a",
                              allowed_hosts=frozenset({f"127.0.0.1:{port}"}), allowed_origins=frozenset()))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    def call(method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api{path}", method=method,
                                     headers={"Authorization": "Bearer u", "Content-Type": "application/json"},
                                     data=_json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        pid = None
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/projects", method="POST",
                                     headers={"Authorization": "Bearer u", "Content-Type": "application/json"},
                                     data=_json.dumps({"name": "параллельно"}).encode())
        with urllib.request.urlopen(req) as r:
            pid = _json.loads(r.read())["id"]
        call("POST", f"/projects/{pid}/assets", {"path": str(fx_plain.path)})
        call("POST", f"/projects/{pid}/revisions/initial")
        paths = [f"/projects/{pid}", f"/projects/{pid}/proposals", f"/projects/{pid}/revisions",
                 f"/projects/{pid}/jobs", f"/projects/{pid}/brief", "/worker"] * 8
        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(lambda p: call("GET", p), paths))
        assert statuses == [200] * len(paths)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_loudness_setting_is_authors(client, project):
    url = f"/api/projects/{project['id']}/settings"
    assert client.get(f"/api/projects/{project['id']}", headers=USER).json()["loudness_lufs"] == -16.0
    assert client.post(url, headers=AGENT, json={"loudness_lufs": -14}).status_code == 403
    assert client.post(url, headers=USER, json={"loudness_lufs": -14}).json()["loudness_lufs"] == -14.0
    # Режим работы не задели
    assert client.get(f"/api/projects/{project['id']}", headers=USER).json()["agent_mode"] == "propose"
    assert client.post(url, headers=USER, json={"loudness_lufs": None}).json()["loudness_lufs"] is None
    assert client.post(url, headers=USER, json={"loudness_lufs": 0}).status_code == 422
    assert client.post(url, headers=USER, json={}).status_code == 422


def test_authors_format_settings(client, project):
    """Формат по образцу: кадр по умолчанию «экран в рамке, лицо сбоку» и звук
    камеры — решения автора; звук камеры — только когда камера сведена."""
    pid = project["id"]
    layout = client.post(f"/api/projects/{pid}/layout", headers=USER, json={"layout": "side", "params": {}})
    assert layout.status_code == 200 and layout.json()["layout"] == "side"
    assert client.post(f"/api/projects/{pid}/layout", headers=AGENT,
                       json={"layout": "side", "params": {}}).status_code == 403
    url = f"/api/projects/{pid}/settings"
    assert client.get(f"/api/projects/{pid}", headers=USER).json()["audio_source"] == "screen"
    assert client.post(url, headers=AGENT, json={"audio_source": "camera"}).status_code == 403
    # Камеры в проекте нет — звук камеры взять неоткуда
    no_cam = client.post(url, headers=USER, json={"audio_source": "camera"})
    assert no_cam.status_code in (400, 409, 422) and "камера" in no_cam.text
    assert client.post(url, headers=USER, json={"audio_source": "screen"}).json()["audio_source"] == "screen"


def test_move_through_http(client, project):
    """Перестановка по HTTP (step-29): Claude — по месту исходника, автор — по
    кадру под курсором; у стыков версии — вид cut или move."""
    pid, cam = project["id"], project["cam"]
    client.post(f"/api/projects/{pid}/settings", headers=USER, json={"agent_mode": "apply"})
    move = {"kind": "move", "asset_id": cam, "src_start_us": us(100), "src_end_us": us(200), "reason": "reorder",
            "why": "автор: блок после таймлайна", "target_src_us": us(600), "target_side": "after"}
    prop = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": project["rev"], "rationale": "перестановка", "ops": [move]})
    assert prop.status_code == 201, prop.json()
    op = prop.json()["ops"][0]
    assert op["kind"] == "move" and op["risk_flags"] == ["reorder"] and op["decision"] == "accepted"
    rid = prop.json()["result_revision_id"]
    kinds = [j["kind"] for j in client.get(f"/api/projects/{pid}/revisions/{rid}/joints", headers=AGENT).json()]
    assert kinds == ["move", "move", "move", "cut"]  # 3 стыка перестановки и переход на второй файл
    # Место — одно; у выреза места нет
    bad = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": rid, "rationale": "x", "ops": [{**move, "target_edited_frame": 10}]})
    assert bad.status_code == 422 and bad.json()["code"] == "no_target"
    bad = client.post(f"/api/projects/{pid}/proposals", headers=AGENT, json={
        "base_revision_id": rid, "rationale": "x",
        "ops": [{**remove(cam, 900, 950), "target_src_us": us(10), "target_side": "after"}]})
    assert bad.status_code == 422 and bad.json()["code"] == "bad_target"
    # Автор на таймлайне: ⌘X кусок 1000–1100, ⌘⇧V перед кадром под курсором (кадр 50 ролика)
    mine = client.post(f"/api/projects/{pid}/proposals", headers=USER, json={
        "base_revision_id": rid, "rationale": "Ручной монтаж на таймлайне", "policy_version": "manual",
        "ops": [{"kind": "move", "asset_id": cam, "src_start_us": us(1000), "src_end_us": us(1100),
                 "reason": "reorder", "why": "переставлено вручную", "precise": True,
                 "target_edited_frame": 50, "target_side": "before"}]}).json()
    done = client.post(f"/api/projects/{pid}/proposals/{mine['id']}/approve", headers=USER, json={
        "acknowledge_op_ids": [o["id"] for o in mine["ops"]]}).json()
    rev = client.get(f"/api/projects/{pid}/revisions/{done['result_revision_id']}", headers=USER).json()
    spans = [(c["src_start_us"], c["src_end_us"]) for c in rev["clips"] if c["asset_id"] == cam]
    assert spans[:3] == [(0, us(50)), (us(1000), us(1100)), (us(50), us(100))]
    assert len(rev["moves"]) == 2
